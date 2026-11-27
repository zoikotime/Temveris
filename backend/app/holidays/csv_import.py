"""
Temveris – Holiday CSV import
==============================
Parsing and validation, kept out of the route so it is unit-testable without an
HTTP client or a database.

What was wrong before
---------------------
* **Silent total failure.** Every row was added to the session and a single
  unguarded ``db.commit()`` ran at the end. Two rows sharing a date inside one
  file slipped past the pre-check (which queried the database, not the pending
  session), the commit raised ``IntegrityError``, Starlette returned a
  plain-text 500, the frontend's ``r.json()`` threw, and the surrounding
  ``catch {}`` swallowed it. The admin saw nothing at all.
* **Ambiguous dates.** ``%d/%m/%Y`` was tried before ``%m/%d/%Y``, so a US-format
  file imported ``03/04/2026`` as 3 April with no warning.
* **Invisible duplicates.** Skipped rows were counted but never explained.
* **No country.** Every row landed in the organization's unscoped calendar.
* **No size limit.** The whole upload was read into memory.

The parser below reports every row it rejects and why, distinguishes duplicates
from invalid data, and refuses ambiguous dates instead of guessing.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

from app.holidays import catalog

#: Upload ceiling. A national calendar is a few hundred rows; anything at this
#: size is a mistake or an attack, and reading it into memory is the risk.
MAX_CSV_BYTES = 2 * 1024 * 1024  # 2 MB
MAX_CSV_ROWS = 5000

DATE_FORMAT_AUTO = "auto"
DATE_FORMAT_ISO = "iso"
DATE_FORMAT_DMY = "dmy"
DATE_FORMAT_MDY = "mdy"

DATE_FORMATS: Tuple[str, ...] = (
    DATE_FORMAT_AUTO, DATE_FORMAT_ISO, DATE_FORMAT_DMY, DATE_FORMAT_MDY,
)

DATE_FORMAT_LABELS = {
    DATE_FORMAT_AUTO: "Detect automatically (rejects ambiguous dates)",
    DATE_FORMAT_ISO: "YYYY-MM-DD",
    DATE_FORMAT_DMY: "DD/MM/YYYY",
    DATE_FORMAT_MDY: "MM/DD/YYYY",
}

#: Accepted column spellings, normalized to lower-case with spaces/underscores
#: stripped, so "Holiday Name", "holiday_name" and "name" all work.
_COLUMN_ALIASES: Dict[str, Tuple[str, ...]] = {
    "name": ("name", "holidayname", "holiday", "title"),
    "date": ("date", "holidaydate", "day"),
    "holiday_type": ("type", "holidaytype", "category"),
    "description": ("description", "notes", "note", "remarks"),
    "country_code": ("countrycode", "country", "iso", "iso2"),
}

_ISO_SEPARATORS = ("-",)


class CsvTooLarge(Exception):
    """Upload exceeded MAX_CSV_BYTES."""


def _canonical(header: Any) -> str:
    return "".join(ch for ch in str(header or "").lower() if ch.isalnum())


def _build_column_map(fieldnames: Optional[List[str]]) -> Dict[str, str]:
    """Map our canonical field names onto whatever the file actually calls them."""
    mapping: Dict[str, str] = {}
    for raw in fieldnames or []:
        canon = _canonical(raw)
        for field, aliases in _COLUMN_ALIASES.items():
            if field in mapping:
                continue
            if canon in aliases:
                mapping[field] = raw
                break
    return mapping


def parse_date(raw: Any, date_format: str) -> Tuple[Optional[date], Optional[str]]:
    """Parse one cell into a date. Returns ``(value, error)``; exactly one is set.

    ``auto`` accepts ISO unconditionally, and a slash/dot format only when the
    value can mean one thing — a first component above 12 is unambiguously a
    day, a second component above 12 unambiguously a month. Genuinely ambiguous
    values like ``03/04/2026`` are REJECTED with an instruction, because
    silently choosing one reading is how the previous importer moved holidays by
    a month without telling anyone.
    """
    text = str(raw or "").strip()
    if not text:
        return None, "date is empty"

    # ISO is unambiguous and always accepted.
    if any(sep in text for sep in _ISO_SEPARATORS) and len(text.split("-")) == 3:
        head = text.split("-")[0]
        if len(head) == 4:
            try:
                return datetime.strptime(text, "%Y-%m-%d").date(), None
            except ValueError:
                return None, f"'{text}' is not a valid YYYY-MM-DD date"

    if date_format == DATE_FORMAT_ISO:
        return None, f"'{text}' is not YYYY-MM-DD (the selected date format)"

    parts = [p for p in text.replace(".", "/").replace("-", "/").split("/") if p != ""]
    if len(parts) != 3:
        return None, f"'{text}' is not a recognised date"
    try:
        first, second, year_part = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None, f"'{text}' is not a recognised date"
    if year_part < 100:
        return None, f"'{text}' uses a 2-digit year — write the year in full"

    def _make(day: int, month: int) -> Tuple[Optional[date], Optional[str]]:
        try:
            return date(year_part, month, day), None
        except ValueError:
            return None, f"'{text}' is not a real calendar date"

    if date_format == DATE_FORMAT_DMY:
        return _make(first, second)
    if date_format == DATE_FORMAT_MDY:
        return _make(second, first)

    # auto
    if first > 12 and second <= 12:
        return _make(first, second)          # can only be D/M
    if second > 12 and first <= 12:
        return _make(second, first)          # can only be M/D
    if first > 12 and second > 12:
        return None, f"'{text}' is not a real calendar date"
    return None, (
        f"'{text}' is ambiguous — it could be D/M/YYYY or M/D/YYYY. "
        "Re-upload with the date format set explicitly, or use YYYY-MM-DD."
    )


def parse_csv(
    content: bytes,
    *,
    date_format: str = DATE_FORMAT_AUTO,
    default_country_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate a holiday CSV without touching the database.

    Returns::

        {
          "rows":       [ {name, date, holiday_type, description, country_code,
                           line} ],           # valid, de-duplicated within file
          "invalid":    [ {line, error, raw} ],
          "duplicates": [ {line, error, raw} ],   # duplicated INSIDE the file
          "columns":    {detected -> actual header},
          "total_rows": int,
        }

    Duplicates *within the file* are separated from rows that will collide with
    existing database records — the caller reports those separately, so an admin
    can tell "my file repeats itself" from "we already have this".
    """
    if len(content) > MAX_CSV_BYTES:
        raise CsvTooLarge(
            f"File is larger than {MAX_CSV_BYTES // (1024 * 1024)} MB."
        )

    if date_format not in DATE_FORMATS:
        date_format = DATE_FORMAT_AUTO

    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1", errors="replace")

    reader = csv.DictReader(io.StringIO(text))
    columns = _build_column_map(reader.fieldnames)

    default_cc = catalog.normalize_country_code(default_country_code)

    rows: List[Dict[str, Any]] = []
    invalid: List[Dict[str, Any]] = []
    duplicates: List[Dict[str, Any]] = []
    seen: Dict[Tuple[Optional[str], str, str], int] = {}
    total = 0

    def _cell(row: Dict[str, Any], field: str) -> str:
        header = columns.get(field)
        if header is None:
            return ""
        return str(row.get(header) or "").strip()

    if "name" not in columns or "date" not in columns:
        missing = [f for f in ("name", "date") if f not in columns]
        raise ValueError(
            "CSV is missing required column(s): " + ", ".join(missing) +
            ". Expected headers: name, date, type, description, country_code."
        )

    for line_no, raw_row in enumerate(reader, start=2):  # line 1 is the header
        total += 1
        if total > MAX_CSV_ROWS:
            invalid.append({
                "line": line_no,
                "error": f"File exceeds the {MAX_CSV_ROWS}-row limit; remaining rows were not read.",
                "raw": "",
            })
            break

        name = _cell(raw_row, "name")
        date_raw = _cell(raw_row, "date")
        raw_label = f"{name or '(no name)'} / {date_raw or '(no date)'}"

        if not name:
            invalid.append({"line": line_no, "error": "name is empty", "raw": raw_label})
            continue

        parsed_date, date_error = parse_date(date_raw, date_format)
        if parsed_date is None:
            invalid.append({"line": line_no, "error": date_error or "invalid date",
                            "raw": raw_label})
            continue

        row_country_raw = _cell(raw_row, "country_code")
        if row_country_raw:
            row_country = catalog.normalize_country_code(row_country_raw)
            if row_country is None:
                invalid.append({
                    "line": line_no,
                    "error": f"'{row_country_raw}' is not a 2-letter ISO country code",
                    "raw": raw_label,
                })
                continue
        else:
            row_country = default_cc

        holiday_type = catalog.normalize_holiday_type(_cell(raw_row, "holiday_type"))
        description = _cell(raw_row, "description")

        key = (row_country, parsed_date.isoformat(), name.casefold())
        if key in seen:
            duplicates.append({
                "line": line_no,
                "error": f"repeats row {seen[key]} in this file",
                "raw": raw_label,
            })
            continue
        seen[key] = line_no

        rows.append({
            "name": name,
            "date": parsed_date,
            "holiday_type": holiday_type,
            "description": description or None,
            "country_code": row_country,
            "line": line_no,
        })

    return {
        "rows": rows,
        "invalid": invalid,
        "duplicates": duplicates,
        "columns": columns,
        "total_rows": total,
    }
