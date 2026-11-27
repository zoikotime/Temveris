"""
Holiday CSV parsing — unit tests for app.holidays.csv_import.

No database and no HTTP: the parser is deliberately separable so the awkward
cases (ambiguous dates, in-file duplicates, junk rows) can be pinned cheaply.
"""
from __future__ import annotations

from datetime import date

import pytest

from app.holidays import csv_import


def _csv(text: str) -> bytes:
    return text.strip().encode("utf-8")


def _parse(text: str, **kwargs):
    return csv_import.parse_csv(_csv(text), **kwargs)


# ── Happy path ────────────────────────────────────────────────────────────────
def test_minimal_valid_file():
    out = _parse("""
name,date
Republic Day,2026-01-26
Independence Day,2026-08-15
""")
    assert len(out["rows"]) == 2
    assert out["invalid"] == [] and out["duplicates"] == []
    assert out["rows"][0]["date"] == date(2026, 1, 26)
    assert out["rows"][0]["holiday_type"] == "Public"


def test_all_columns():
    out = _parse("""
name,date,type,description,country_code
Diwali,2026-11-08,Public,Festival of lights,in
""")
    row = out["rows"][0]
    assert row["name"] == "Diwali"
    assert row["holiday_type"] == "Public"
    assert row["description"] == "Festival of lights"
    assert row["country_code"] == "IN"


@pytest.mark.parametrize("header", [
    "Name,Date", "NAME,DATE", "Holiday Name,Holiday Date", "holiday_name,holiday_date",
])
def test_header_aliases_and_casing(header):
    out = _parse(f"{header}\nRepublic Day,2026-01-26")
    assert len(out["rows"]) == 1


def test_utf8_bom_is_stripped():
    content = "﻿name,date\nRepublic Day,2026-01-26".encode("utf-8")
    out = csv_import.parse_csv(content)
    assert len(out["rows"]) == 1
    assert out["rows"][0]["name"] == "Republic Day"


def test_latin1_fallback_does_not_crash():
    content = "name,date\nCaf\xe9 Day,2026-01-26".encode("latin-1")
    out = csv_import.parse_csv(content)
    assert len(out["rows"]) == 1


# ── Required columns ──────────────────────────────────────────────────────────
def test_missing_required_columns_is_a_clear_error():
    with pytest.raises(ValueError) as exc:
        _parse("holiday,when\nDiwali,2026-11-08")
    assert "name" in str(exc.value) and "date" in str(exc.value)


# ── Date handling ─────────────────────────────────────────────────────────────
def test_iso_dates_always_accepted():
    out = _parse("name,date\nX,2026-03-04", date_format=csv_import.DATE_FORMAT_AUTO)
    assert out["rows"][0]["date"] == date(2026, 3, 4)


def test_ambiguous_slash_date_is_rejected_not_guessed():
    """The audit's finding: 03/04/2026 was silently read as 3 April because
    %d/%m/%Y was tried first. A US-format file imported wrong dates with no
    warning at all."""
    out = _parse("name,date\nX,03/04/2026", date_format=csv_import.DATE_FORMAT_AUTO)
    assert out["rows"] == []
    assert len(out["invalid"]) == 1
    assert "ambiguous" in out["invalid"][0]["error"].lower()


def test_unambiguous_slash_date_is_accepted_in_auto():
    # 25 cannot be a month, so this can only be D/M/YYYY.
    out = _parse("name,date\nX,25/12/2026", date_format=csv_import.DATE_FORMAT_AUTO)
    assert out["rows"][0]["date"] == date(2026, 12, 25)
    # 12/25 can only be M/D/YYYY.
    out = _parse("name,date\nX,12/25/2026", date_format=csv_import.DATE_FORMAT_AUTO)
    assert out["rows"][0]["date"] == date(2026, 12, 25)


def test_explicit_dmy_resolves_the_ambiguity():
    out = _parse("name,date\nX,03/04/2026", date_format=csv_import.DATE_FORMAT_DMY)
    assert out["rows"][0]["date"] == date(2026, 4, 3)


def test_explicit_mdy_resolves_the_ambiguity_the_other_way():
    out = _parse("name,date\nX,03/04/2026", date_format=csv_import.DATE_FORMAT_MDY)
    assert out["rows"][0]["date"] == date(2026, 3, 4)


def test_iso_mode_rejects_slash_dates():
    out = _parse("name,date\nX,03/04/2026", date_format=csv_import.DATE_FORMAT_ISO)
    assert out["rows"] == []
    assert "YYYY-MM-DD" in out["invalid"][0]["error"]


@pytest.mark.parametrize("bad", ["", "not-a-date", "2026-13-45", "31/02/2026", "1/1/26"])
def test_invalid_dates_are_reported_with_the_line_number(bad):
    out = _parse(f"name,date\nX,{bad}")
    assert out["rows"] == []
    assert out["invalid"][0]["line"] == 2
    assert out["invalid"][0]["error"]


def test_two_digit_year_is_rejected():
    out = _parse("name,date\nX,01/01/26")
    assert "2-digit year" in out["invalid"][0]["error"]


# ── Row validation ────────────────────────────────────────────────────────────
def test_empty_name_is_invalid():
    out = _parse("name,date\n,2026-01-26")
    assert out["rows"] == []
    assert "name is empty" in out["invalid"][0]["error"]


def test_bad_country_code_is_invalid():
    out = _parse("name,date,country_code\nX,2026-01-26,INDIA")
    assert out["rows"] == []
    assert "ISO country code" in out["invalid"][0]["error"]


def test_row_country_overrides_the_default():
    out = _parse("name,date,country_code\nX,2026-01-26,US\nY,2026-02-01,",
                 default_country_code="IN")
    assert out["rows"][0]["country_code"] == "US"
    assert out["rows"][1]["country_code"] == "IN"


def test_no_country_anywhere_stays_unscoped():
    """The country is never guessed from the organization — an unscoped row is
    an explicit, visible state."""
    out = _parse("name,date\nX,2026-01-26")
    assert out["rows"][0]["country_code"] is None


def test_unknown_type_normalizes_rather_than_failing_the_row():
    out = _parse("name,date,type\nX,2026-01-26,Bank Holiday")
    assert out["rows"][0]["holiday_type"] == "Public"


# ── Duplicates within the file ────────────────────────────────────────────────
def test_duplicate_rows_inside_the_file_are_caught():
    """This is what produced the unexplained 500: the old pre-check queried the
    DATABASE, so two identical rows in one upload both passed and the commit
    blew up at the end."""
    out = _parse("""
name,date
Diwali,2026-11-08
Diwali,2026-11-08
""")
    assert len(out["rows"]) == 1
    assert len(out["duplicates"]) == 1
    assert "repeats row 2" in out["duplicates"][0]["error"]


def test_duplicate_detection_is_case_insensitive_on_name():
    out = _parse("name,date\nDiwali,2026-11-08\ndiwali,2026-11-08")
    assert len(out["rows"]) == 1
    assert len(out["duplicates"]) == 1


def test_same_date_different_name_is_not_a_duplicate():
    """Gandhi Jayanti and Dussehra shared 2025-10-02."""
    out = _parse("name,date\nGandhi Jayanti,2025-10-02\nDussehra,2025-10-02")
    assert len(out["rows"]) == 2
    assert out["duplicates"] == []


def test_same_date_and_name_in_different_countries_is_not_a_duplicate():
    out = _parse("""
name,date,country_code
New Year's Day,2026-01-01,US
New Year's Day,2026-01-01,IN
""")
    assert len(out["rows"]) == 2
    assert out["duplicates"] == []


# ── Limits ────────────────────────────────────────────────────────────────────
def test_oversized_file_is_rejected_before_parsing():
    payload = b"name,date\n" + b"X,2026-01-01\n" * 400_000
    assert len(payload) > csv_import.MAX_CSV_BYTES
    with pytest.raises(csv_import.CsvTooLarge):
        csv_import.parse_csv(payload)


def test_row_limit_stops_parsing_and_says_so():
    lines = "\n".join(f"H{i},2026-01-01" for i in range(csv_import.MAX_CSV_ROWS + 10))
    out = _parse(f"name,date\n{lines}")
    assert len(out["rows"]) <= csv_import.MAX_CSV_ROWS
    assert any("row limit" in item["error"] for item in out["invalid"])


# ── Mixed file: the realistic case ────────────────────────────────────────────
def test_mixed_file_separates_valid_invalid_and_duplicate():
    out = _parse("""
name,date,type
Republic Day,2026-01-26,Public
,2026-02-01,Public
Bad Date,nonsense,Public
Republic Day,2026-01-26,Public
Diwali,2026-11-08,Optional
""")
    assert [r["name"] for r in out["rows"]] == ["Republic Day", "Diwali"]
    assert len(out["invalid"]) == 2
    assert len(out["duplicates"]) == 1
    assert out["total_rows"] == 5
