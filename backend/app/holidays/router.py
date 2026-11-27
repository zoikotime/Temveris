"""
Temveris – Holiday Management Router
=====================================
The one HTTP surface for holidays:

  * the **country catalog** — which countries we can serve, and the calendar for
    a country + year (curated for India / UAE, Nager.Date for the rest);
  * the **organization calendar** — CRUD over the ``holidays`` table, now
    country-scoped;
  * **CSV import**, country-aware and with a structured result.

The route layer only validates, authorizes and shapes responses. Catalog
resolution lives in ``service.py``, the provider in ``provider.py``, the curated
calendars in ``catalog.py``, database reads in ``org_calendar.py``, CSV parsing
in ``csv_import.py``, and the role policy in ``permissions.py``. Nothing here
reaches past the layer below it, so the external provider is never part of the
API contract and the browser never talks to it.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field, field_validator
from typing import Optional, List, Dict, Any
from datetime import datetime
import logging

from database.database import get_db
from database import models
from app.auth.service import get_current_user, get_current_organization
from app.holidays import catalog, collisions, csv_import, org_calendar, service
from app.holidays.permissions import holiday_admin

router = APIRouter(prefix="/holidays", tags=["Holidays"])

_log = logging.getLogger("zoikotime.holidays.router")


# ── Request models ────────────────────────────────────────────────────────────
class HolidayIn(BaseModel):
    name: str
    date: str                                   # YYYY-MM-DD
    holiday_type: str = catalog.DEFAULT_HOLIDAY_TYPE
    description: Optional[str] = None
    # ISO-2. Omitted / null means an unscoped holiday that applies to every
    # calendar in the organization — the shape every pre-migration row has.
    country_code: Optional[str] = Field(default=None, max_length=2)


class ImportCatalogIn(BaseModel):
    """Adopt a catalog calendar into the organization's own holidays.

    Defaults are deliberately the conservative ones: add only holidays the
    organization does not already observe, nationwide, mandatory.
    """
    country_code: str = Field(min_length=2, max_length=2)
    year: int
    # Optional/restricted days are not organization holidays by default —
    # importing them would mark non-gazetted festivals non-working for
    # everyone. The admin opts in.
    include_optional: bool = False
    # Regional days are observed in SOME subdivisions only. Adding them applies
    # them to every employee, which is wrong for a Scotland-only bank holiday.
    include_regional: bool = False
    # Per-row adoption: restrict the run to these entry keys
    # ("YYYY-MM-DD|canonical name" — see collisions.entry_key). Empty = all.
    keys: Optional[List[str]] = None
    # Explicit admin decisions for DATE_CONFLICT rows, keyed the same way.
    # "skip" (default) | "replace" (update the existing row) | "add_anyway".
    resolutions: Optional[Dict[str, str]] = None

    @field_validator("resolutions")
    @classmethod
    def _valid_resolutions(cls, value):
        if not value:
            return value
        allowed = {"skip", "replace", "add_anyway"}
        bad = {k: v for k, v in value.items() if v not in allowed}
        if bad:
            raise ValueError(
                f"resolution must be one of {sorted(allowed)}; got {bad}")
        return value


# ── Helpers ───────────────────────────────────────────────────────────────────
def _parse_date(raw: str):
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid date. Use YYYY-MM-DD")


def _clean_country(raw: Optional[str], *, field: str = "country_code") -> Optional[str]:
    """None stays None (unscoped); anything present must be a real ISO-2 code."""
    if raw is None or str(raw).strip() == "":
        return None
    code = catalog.normalize_country_code(raw)
    if code is None:
        raise HTTPException(
            status_code=400,
            detail=f"{field} must be a 2-letter ISO 3166-1 alpha-2 code",
        )
    return code


def _validated_name(raw: str) -> str:
    name = (raw or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Holiday name is required")
    if len(name) > 255:
        raise HTTPException(status_code=400, detail="Holiday name is too long (max 255 characters)")
    return name


def _catalog_error(exc: service.CatalogError) -> HTTPException:
    """Turn a catalog failure into a response the UI can act on.

    ``kind`` travels in the body so the frontend can choose its own wording and,
    more importantly, tell "this country has no external calendar" apart from
    "the provider is down" — the distinction the old single red string erased.
    """
    return HTTPException(
        status_code=exc.status_code,
        detail={"kind": exc.kind, "message": exc.message},
    )


# ── Seeding ───────────────────────────────────────────────────────────────────
# A brand-new organization would otherwise open an empty Public Holidays page.
# We seed the calendar for ITS OWN country, from the shared catalog, once.
#
# The country check matters: before it existed, every tenant was seeded with the
# Indian gazetted calendar regardless of where it operated, and the audit found
# two US organizations still carrying Diwali and Republic Day as non-working
# days. Seeding an unknown country with nothing is correct; seeding it with some
# other country's holidays is not.
HOLIDAY_SEED_MARKER_KEY = "holidays_default_seeded"


def _seed_default_holidays(db: Session, org_id: Optional[int]) -> None:
    """Seed this organization's own country calendar, at most once, ever.

    Resilient by design: any failure rolls back and never breaks the read that
    triggered it.
    """
    if org_id is None:
        return
    try:
        country = org_calendar.org_primary_country(db, org_id)
        if not country or not catalog.has_curated_calendar(country):
            # We only auto-seed from curated calendars. Reaching out to the
            # external provider during a GET would put a network round-trip in
            # the request path and could seed a tenant from a source that was
            # unreachable a minute later.
            return

        has_any = db.query(models.Holiday.id).filter(
            models.Holiday.organization_id == org_id
        ).first()
        if has_any:
            return
        already = db.query(models.AppSettings.id).filter(
            models.AppSettings.organization_id == org_id,
            models.AppSettings.key == HOLIDAY_SEED_MARKER_KEY,
        ).first()
        if already:
            # Seeded once and then deliberately emptied by an admin. Leave it.
            return

        year = datetime.utcnow().year
        calendar_payload = catalog.curated_calendar(country, year)
        if not calendar_payload or not calendar_payload["holidays"]:
            return

        for entry in calendar_payload["holidays"]:
            if entry["holiday_type"] == catalog.HOLIDAY_TYPE_OPTIONAL:
                # Restricted / observance days are not organization holidays by
                # default — they would mark working days non-working.
                continue
            db.add(models.Holiday(
                name=entry["name"],
                date=datetime.strptime(entry["date"], "%Y-%m-%d").date(),
                holiday_type=entry["holiday_type"],
                description=entry["description"] or None,
                organization_id=org_id,
                country_code=country,
            ))
        db.add(models.AppSettings(
            organization_id=org_id,
            key=HOLIDAY_SEED_MARKER_KEY,
            value="true",
            description="Shipped public-holiday calendar seeded for this org",
        ))
        db.commit()
    except Exception:
        _log.warning("holidays seed failed org=%s", org_id, exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


# ── COUNTRY CATALOG ───────────────────────────────────────────────────────────
@router.get("/countries")
def list_countries(
    current_user: models.User = Depends(get_current_user),
):
    """Countries the admin may pick, and which engine serves each.

    Replaces the browser's direct call to the provider's /AvailableCountries.
    Curated countries (India, UAE) are merged in, so they are selectable even
    when the provider is unreachable — the case that previously produced an
    empty dropdown.
    """
    return service.list_supported_countries()


@router.get("/catalog")
def country_catalog(
    country_code: str = Query(..., min_length=2, max_length=2),
    year: int = Query(...),
    current_user: models.User = Depends(get_current_user),
):
    """The public-holiday calendar for a country + year.

    Read-only reference data — this is a preview, not the organization's own
    calendar. Adopt it with ``POST /holidays/import-catalog``.
    """
    try:
        return service.get_country_calendar(country_code, year)
    except service.CatalogError as exc:
        raise _catalog_error(exc)


# ── ORGANIZATION CALENDAR: LIST ───────────────────────────────────────────────
@router.get("")
def list_holidays(
    year: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """The organization's own holidays.

    Both filters are optional and BOTH DEFAULT TO OFF, which is the pre-existing
    contract: with no parameters this returns every holiday the organization
    holds, exactly as before. The employee Dashboard, the Upcoming Holidays card
    and any other existing consumer are therefore unaffected.

    ``country_code`` matches that country **plus unscoped rows** (country_code
    IS NULL), so holidays that predate country-scoped calendars stay visible
    under every filter instead of vanishing from the admin's view.
    """
    _seed_default_holidays(db, current_org.id)

    if year is not None and not catalog.is_valid_year(year):
        raise HTTPException(
            status_code=400,
            detail=f"Year must be between {catalog.MIN_YEAR} and {catalog.MAX_YEAR}",
        )
    code = _clean_country(country_code)

    return org_calendar.list_org_holidays(
        db, current_org.id, year=year, country_code=code
    )


@router.get("/summary")
def holidays_summary(
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """Which calendars and years this organization actually maintains.

    Lets the admin page populate its Country and Year selectors from real data
    instead of a hardcoded range.
    """
    return {
        "primary_country": org_calendar.org_primary_country(db, current_org.id),
        "country_codes": org_calendar.distinct_country_codes(db, current_org.id),
        "years": list(org_calendar.iter_years(db, current_org.id)),
    }


# ── CREATE ────────────────────────────────────────────────────────────────────
@router.post("")
def create_holiday(
    data: HolidayIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    parsed_date = _parse_date(data.date)
    name = _validated_name(data.name)
    country = _clean_country(data.country_code)
    holiday_type = catalog.normalize_holiday_type(data.holiday_type)

    # Duplicate detection mirrors the database key: (country, date, name).
    # Deliberately NOT "a holiday already exists on this date" — two countries
    # legitimately share a date, and so do two distinct holidays in one country
    # (Gandhi Jayanti and Dussehra both fell on 2025-10-02).
    if org_calendar.find_duplicate(
        db, current_user.organization_id, parsed_date, name, country
    ):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists on {parsed_date.isoformat()} for this calendar",
        )

    new_holiday = models.Holiday(
        name=name,
        date=parsed_date,
        holiday_type=holiday_type,
        description=(data.description or "").strip() or None,
        organization_id=current_user.organization_id,
        country_code=country,
    )
    db.add(new_holiday)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent insert, or a database still carrying an older, narrower
        # unique index. Either way the caller gets a clean 409 rather than a
        # plain-text 500 the frontend cannot parse.
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists on {parsed_date.isoformat()} for this calendar",
        )
    db.refresh(new_holiday)
    return {
        "id": new_holiday.id,
        "message": "Holiday created",
        "holiday": _serialize(new_holiday),
    }


def _serialize(row: models.Holiday) -> Dict[str, Any]:
    entry = catalog.normalize_entry(
        {
            "id": row.id,
            "name": row.name,
            "date": row.date.strftime("%Y-%m-%d") if row.date else "",
            "holiday_type": row.holiday_type,
            "description": row.description,
        },
        source="organization",
    )
    entry["country_code"] = row.country_code
    entry["created_at"] = row.created_at.isoformat() if row.created_at else None
    return entry


# ── UPDATE ────────────────────────────────────────────────────────────────────
@router.put("/{hid}")
def update_holiday(
    hid: int,
    data: HolidayIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    parsed_date = _parse_date(data.date)
    name = _validated_name(data.name)
    country = _clean_country(data.country_code)
    holiday_type = catalog.normalize_holiday_type(data.holiday_type)

    holiday = db.query(models.Holiday).filter(models.Holiday.id == hid).first()
    # A foreign organization's holiday is reported as absent, not as forbidden:
    # a 403 would confirm the id exists in another tenant.
    if not holiday or holiday.organization_id != current_user.organization_id:
        raise HTTPException(status_code=404, detail="Holiday not found")

    if org_calendar.find_duplicate(
        db, current_user.organization_id, parsed_date, name, country, exclude_id=hid
    ):
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists on {parsed_date.isoformat()} for this calendar",
        )

    holiday.name = name
    holiday.date = parsed_date
    holiday.holiday_type = holiday_type
    holiday.description = (data.description or "").strip() or None
    holiday.country_code = country
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"'{name}' already exists on {parsed_date.isoformat()} for this calendar",
        )
    db.refresh(holiday)
    return {"message": "Holiday updated", "holiday": _serialize(holiday)}


# ── DELETE ────────────────────────────────────────────────────────────────────
@router.delete("/{hid}")
def delete_holiday(
    hid: int,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    """Hard delete, scoped to the caller's organization.

    Previously this returned 200 "Holiday deleted" whether or not anything was
    deleted — a wrong id and another tenant's id both reported success, and the
    UI showed a green toast for a no-op. Missing and cross-tenant now both 404.
    """
    holiday = db.query(models.Holiday).filter(models.Holiday.id == hid).first()
    if not holiday or holiday.organization_id != current_user.organization_id:
        raise HTTPException(status_code=404, detail="Holiday not found")

    db.delete(holiday)
    db.commit()
    return {"message": "Holiday deleted", "id": hid}


# ── ADOPT A CATALOG CALENDAR ──────────────────────────────────────────────────
def _eligibility(entry: Dict[str, Any], include_optional: bool,
                 include_regional: bool) -> Optional[str]:
    """Why this entry is not adoptable by default, or None if it is.

    Independent of collision status: an Optional or regional holiday is
    filtered on what it IS, not on what the organization already has.
    """
    if entry["holiday_type"] == catalog.HOLIDAY_TYPE_OPTIONAL and not include_optional:
        return "optional/restricted holiday — enable 'include optional' to add"
    if entry.get("regional") and not include_regional:
        regions = ", ".join(entry.get("regions") or [])
        return (f"observed only in {regions} — enable 'include regional' to add"
                if regions else
                "regional holiday — enable 'include regional' to add")
    return None


def _adoption_plan(db: Session, org_id, body_country: str, year: int,
                   include_optional: bool, include_regional: bool):
    """The classified calendar. Shared by the preview route and the writer, so
    what the admin is shown is exactly what the write will do."""
    try:
        payload = service.get_country_calendar(body_country, year)
    except service.CatalogError as exc:
        raise _catalog_error(exc)

    country = payload["country_code"]
    classified = collisions.classify_entries(db, org_id, country, payload["holidays"])

    for item in classified:
        item["key"] = collisions.entry_key(item)
        blocked = _eligibility(item, include_optional, include_regional)
        item["eligible"] = blocked is None
        if blocked:
            item["reason"] = blocked
    return payload, country, classified


@router.get("/catalog/plan")
def catalog_adoption_plan(
    country_code: str = Query(..., min_length=2, max_length=2),
    year: int = Query(...),
    include_optional: bool = Query(False),
    include_regional: bool = Query(False),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    """What adopting this calendar WOULD do — classified, nothing written.

    The admin page previously computed "available to add" in the browser by
    de-duplicating on date+name, while the backend de-duplicated on
    (country, date, name). Because every pre-migration holiday has
    ``country_code IS NULL``, the backend matched nothing and the two
    disagreed: the UI offered 13 and the server inserted 20. Both sides now
    read this one endpoint, so the count on screen is the count that happens.
    """
    payload, country, classified = _adoption_plan(
        db, current_user.organization_id, country_code, year,
        include_optional, include_regional,
    )
    counts = collisions.summarize(classified)
    counts["not_eligible"] = sum(1 for i in classified if not i["eligible"])
    counts["will_add"] = sum(
        1 for i in classified
        if i["eligible"] and i["status"] == collisions.STATUS_NEW
    )
    return {
        "country_code": country,
        "country_name": payload["country_name"],
        "year": payload["year"],
        "source": payload["source"],
        "partial": payload["partial"],
        "counts": counts,
        "entries": classified,
    }


@router.post("/import-catalog")
def import_catalog(
    body: ImportCatalogIn,
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    """Copy a country's catalog calendar into the organization's own holidays.

    Adds ONLY holidays classified ``new``. Exact duplicates and already-covered
    entries are skipped; date conflicts are skipped unless the admin has sent an
    explicit resolution for that key. The same classification runs here as in
    ``GET /holidays/catalog/plan`` — the browser cannot talk the server into
    writing something the plan did not show, because the server re-classifies
    rather than trusting anything sent to it.

    ``keys`` restricts the run to specific entries, which is how the per-row Add
    button works. It is the same code path, not a second implementation.
    """
    org_id = current_user.organization_id
    payload, country, classified = _adoption_plan(
        db, org_id, body.country_code, body.year,
        body.include_optional, body.include_regional,
    )

    wanted = set(body.keys) if body.keys else None
    resolutions = body.resolutions or {}

    added, replaced = 0, 0
    counts = {s: 0 for s in (collisions.STATUS_NEW, collisions.STATUS_ALREADY_COVERED,
                             collisions.STATUS_EXACT_DUPLICATE,
                             collisions.STATUS_DATE_CONFLICT)}
    skipped_detail: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []

    def _skip(item, reason):
        skipped_detail.append({
            "key": item["key"], "name": item["name"], "date": item["date"],
            "status": item["status"], "reason": reason,
            "existing": item.get("existing"),
        })

    for item in classified:
        counts[item["status"]] = counts.get(item["status"], 0) + 1

        if wanted is not None and item["key"] not in wanted:
            continue
        if not item["eligible"]:
            _skip(item, item["reason"])
            continue

        decision = resolutions.get(item["key"], "skip")
        entry_date = datetime.strptime(item["date"], "%Y-%m-%d").date()

        if item["status"] == collisions.STATUS_DATE_CONFLICT:
            conflicts.append({
                "key": item["key"], "name": item["name"], "date": item["date"],
                "reason": item["reason"], "existing": item.get("existing"),
                "decision": decision,
            })
            if decision == "skip":
                _skip(item, item["reason"])
                continue
            if decision == "replace":
                existing_id = (item.get("existing") or {}).get("id")
                row = db.query(models.Holiday).filter(
                    models.Holiday.id == existing_id,
                    models.Holiday.organization_id == org_id,
                ).first() if existing_id else None
                if row is None:
                    _skip(item, "the holiday it would replace no longer exists")
                    continue
                row.name = item["name"]
                row.date = entry_date
                row.holiday_type = item["holiday_type"]
                row.description = item["description"] or None
                row.country_code = country
                replaced += 1
                continue
            # "add_anyway" falls through to the insert below — the caller has
            # explicitly accepted a second non-working day.

        elif item["status"] != collisions.STATUS_NEW:
            # Exact duplicate / already covered are never written, with or
            # without a resolution. Removing an existing holiday is a delete,
            # not an import, and stays a separate deliberate action.
            _skip(item, item["reason"])
            continue

        db.add(models.Holiday(
            name=item["name"],
            date=entry_date,
            holiday_type=item["holiday_type"],
            description=item["description"] or None,
            organization_id=org_id,
            country_code=country,
        ))
        added += 1

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        _log.warning("holidays import-catalog integrity error org=%s country=%s",
                     org_id, country)
        raise HTTPException(
            status_code=409,
            detail="Import collided with an existing holiday and was rolled back. "
                   "Nothing was added.",
        )

    return {
        "country_code": country,
        "country_name": payload["country_name"],
        "year": payload["year"],
        "source": payload["source"],
        "partial": payload["partial"],
        # What actually happened.
        "added": added,
        "replaced": replaced,
        "skipped": len(skipped_detail),
        # What the calendar looks like, whether or not it was in scope.
        "already_covered": counts[collisions.STATUS_ALREADY_COVERED],
        "exact_duplicates": counts[collisions.STATUS_EXACT_DUPLICATE],
        "conflicts": counts[collisions.STATUS_DATE_CONFLICT],
        "conflict_detail": conflicts[:25],
        "skipped_detail": skipped_detail[:25],
        # Legacy alias — older clients read `imported`.
        "imported": added,
    }


# ── CSV IMPORT ────────────────────────────────────────────────────────────────
@router.get("/import-csv/options")
def csv_options(current_user: models.User = Depends(holiday_admin)):
    """What the importer accepts — drives the upload dialog without hardcoding."""
    return {
        "max_bytes": csv_import.MAX_CSV_BYTES,
        "max_rows": csv_import.MAX_CSV_ROWS,
        "date_formats": [
            {"value": value, "label": csv_import.DATE_FORMAT_LABELS[value]}
            for value in csv_import.DATE_FORMATS
        ],
        "required_columns": ["name", "date"],
        "optional_columns": ["type", "description", "country_code"],
        "holiday_types": list(catalog.HOLIDAY_TYPES),
    }


@router.post("/import-csv")
async def import_csv(
    file: UploadFile = File(...),
    date_format: str = Form(csv_import.DATE_FORMAT_AUTO),
    country_code: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    current_user: models.User = Depends(holiday_admin),
):
    """Import holidays from CSV.

    Columns: ``name`` and ``date`` are required; ``type``, ``description`` and
    ``country_code`` are optional. A row's own ``country_code`` wins; otherwise
    the ``country_code`` form field applies to the whole file; otherwise rows
    are imported unscoped. The country is never guessed from the organization.

    ``date_format`` is one of ``auto`` / ``iso`` / ``dmy`` / ``mdy``. ``auto``
    accepts ISO always and a slash format only when it can mean one thing —
    ``03/04/2026`` is rejected with an instruction rather than silently read as
    3 April.

    The whole import is ONE transaction: on an unexpected database error nothing
    is written and the caller is told, instead of the previous plain-text 500
    that the frontend swallowed in a bare ``catch {}``.
    """
    default_country = _clean_country(country_code)

    content = await file.read()
    try:
        parsed = csv_import.parse_csv(
            content, date_format=date_format, default_country_code=default_country
        )
    except csv_import.CsvTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    org_id = current_user.organization_id
    imported = 0
    existing_duplicates: List[Dict[str, Any]] = []

    try:
        for row in parsed["rows"]:
            if org_calendar.find_duplicate(
                db, org_id, row["date"], row["name"], row["country_code"]
            ):
                existing_duplicates.append({
                    "line": row["line"],
                    "error": "already in this calendar",
                    "raw": f"{row['name']} / {row['date'].isoformat()}",
                })
                continue
            db.add(models.Holiday(
                name=row["name"],
                date=row["date"],
                holiday_type=row["holiday_type"],
                description=row["description"],
                organization_id=org_id,
                country_code=row["country_code"],
            ))
            imported += 1
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="Import failed on a duplicate holiday and was rolled back. "
                   "No rows were imported.",
        )
    except Exception:
        db.rollback()
        _log.exception("holidays CSV import failed org=%s", org_id)
        raise HTTPException(
            status_code=500,
            detail="Import failed and was rolled back. No rows were imported.",
        )

    duplicates = parsed["duplicates"] + existing_duplicates
    invalid = parsed["invalid"]
    return {
        "imported": imported,
        "duplicates": len(duplicates),
        "invalid": len(invalid),
        "skipped": len(duplicates) + len(invalid),
        "total_rows": parsed["total_rows"],
        "date_format": date_format,
        "country_code": default_country,
        # Capped for the response; the counts above are complete.
        "duplicate_detail": duplicates[:25],
        "invalid_detail": invalid[:25],
        # Legacy key: the previous response used `added`, and older clients read
        # it. Kept as an alias so nothing breaks mid-deploy.
        "added": imported,
    }


# ── LEAVE-EXCLUSION HELPER ────────────────────────────────────────────────────
@router.get("/dates")
def holiday_dates(
    year: Optional[int] = None,
    country_code: Optional[str] = None,
    db: Session = Depends(get_db),
    current_org: models.Organization = Depends(get_current_organization),
):
    """Holiday dates as ``YYYY-MM-DD`` strings, for day-count calculations.

    The audit found this endpoint had zero callers while leave day cost was
    computed as a raw calendar span. It is now backed by
    ``org_calendar.holiday_dates`` — the same function the leave engine and the
    schedule resolver use — so the HTTP helper and the in-process calculation
    can no longer disagree. Kept rather than deleted: it is a documented public
    route, and it is now a real view of the authoritative logic.
    """
    if year is not None and not catalog.is_valid_year(year):
        raise HTTPException(
            status_code=400,
            detail=f"Year must be between {catalog.MIN_YEAR} and {catalog.MAX_YEAR}",
        )
    code = _clean_country(country_code)
    dates = org_calendar.holiday_dates(
        db, current_org.id, year=year, country_code=code
    )
    return {"dates": sorted(d.strftime("%Y-%m-%d") for d in dates)}
