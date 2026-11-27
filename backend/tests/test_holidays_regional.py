"""
Regional applicability, kept separate from holiday type.

The defect: ``provider._normalize_provider_holiday`` encoded "this is regional"
by DOWNGRADING ``holiday_type`` to ``Optional``. One field carrying two
independent facts destroyed both:

  * UK New Year's Day lists all four constituent countries — nationwide — yet
    became ``Optional`` and was skipped on adoption. Only 5 of 14 UK holidays,
    6 of 27 Australian and 8 of 31 Canadian ones survived "Add all".
  * India's New Year's Day is genuinely ``Optional`` (restricted) and
    nationwide, yet the Settings page rendered it as "Regional".

``regional`` / ``regions`` are now their own fields.
"""
from __future__ import annotations

import pytest

from app.holidays import catalog, provider


def _gb_payload():
    """Shaped exactly like Nager.Date's real GB response."""
    return [
        {"date": "2026-01-01", "localName": "New Year's Day", "name": "New Year's Day",
         "types": ["Public"], "counties": ["GB-ENG", "GB-NIR", "GB-SCT", "GB-WLS"]},
        {"date": "2026-01-02", "localName": "2 January", "name": "2 January",
         "types": ["Public"], "counties": ["GB-SCT"]},
        {"date": "2026-03-17", "localName": "St. Patrick's Day", "name": "St. Patrick's Day",
         "types": ["Public"], "counties": ["GB-NIR"]},
        {"date": "2026-12-25", "localName": "Christmas Day", "name": "Christmas Day",
         "types": ["Public"], "counties": None},
        {"date": "2026-02-14", "localName": "Valentine's Day", "name": "Valentine's Day",
         "types": ["Observance"], "counties": None},
    ]


@pytest.fixture(autouse=True)
def _clean():
    provider.reset_caches()
    yield
    provider.reset_caches()


def _fetch_gb(monkeypatch):
    class _Resp:
        status_code = 200
        @staticmethod
        def json():
            return _gb_payload()
    monkeypatch.setattr(provider, "_get", lambda path: _Resp())
    return {h["name"]: h for h in provider.fetch_holidays("GB", 2026)}


# ── The two fields are independent ───────────────────────────────────────────
def test_normalized_entries_always_carry_both_fields():
    entry = catalog.normalize_entry(
        {"name": "X", "date": "2026-01-01"}, source="curated")
    assert entry["regional"] is False
    assert entry["regions"] == []


def test_a_holiday_can_be_optional_and_nationwide():
    """India's New Year's Day. Optional does NOT imply regional."""
    entry = next(h for h in catalog.curated_calendar("IN", 2026)["holidays"]
                 if h["name"] == "New Year's Day")
    assert entry["holiday_type"] == "Optional"
    assert entry["regional"] is False, "restricted is not the same as regional"


def test_no_curated_holiday_is_marked_regional():
    """We ship national calendars; none of them carry subdivision data."""
    for cc in ("IN", "AE"):
        for h in catalog.curated_calendar(cc, 2026)["holidays"]:
            assert h["regional"] is False, f"{cc} {h['name']}"


# ── Provider: full county coverage means nationwide ──────────────────────────
def test_uk_new_years_day_is_public_and_nationwide(monkeypatch):
    """It enumerates all four subdivisions. It is not a regional holiday."""
    gb = _fetch_gb(monkeypatch)
    entry = gb["New Year's Day"]
    assert entry["holiday_type"] == "Public", "must not be downgraded to Optional"
    assert entry["regional"] is False
    assert entry["regions"] == []


def test_a_scotland_only_holiday_is_regional_but_still_public(monkeypatch):
    gb = _fetch_gb(monkeypatch)
    entry = gb["2 January"]
    assert entry["holiday_type"] == "Public", "regionality is not a type"
    assert entry["regional"] is True
    assert entry["regions"] == ["GB-SCT"]


def test_a_holiday_with_no_counties_is_nationwide(monkeypatch):
    gb = _fetch_gb(monkeypatch)
    assert gb["Christmas Day"]["regional"] is False
    assert gb["Christmas Day"]["holiday_type"] == "Public"


def test_the_provider_type_vocabulary_still_maps(monkeypatch):
    gb = _fetch_gb(monkeypatch)
    assert gb["Valentine's Day"]["holiday_type"] == "Optional"
    assert gb["Valentine's Day"]["regional"] is False


def test_most_uk_holidays_are_now_adoptable(monkeypatch):
    """Before the fix every entry with a counties array became Optional, so
    'Add all' imported a minority of the calendar."""
    gb = _fetch_gb(monkeypatch)
    nationwide_public = [h for h in gb.values()
                         if h["holiday_type"] == "Public" and not h["regional"]]
    assert len(nationwide_public) == 2  # New Year's Day + Christmas Day
    assert "New Year's Day" in {h["name"] for h in nationwide_public}


def test_regional_detection_degrades_safely_without_the_union():
    """Called with no subdivision context, any counties value stays regional —
    the previous, conservative behaviour."""
    entry = provider._normalize_provider_holiday(
        {"date": "2026-01-01", "localName": "X", "name": "X",
         "types": ["Public"], "counties": ["GB-ENG", "GB-SCT"]},
        all_subdivisions=None,
    )
    assert entry["regional"] is True


def test_a_superset_of_the_known_subdivisions_counts_as_nationwide():
    entry = provider._normalize_provider_holiday(
        {"date": "2026-01-01", "localName": "X", "name": "X",
         "types": ["Public"], "counties": ["A", "B", "C"]},
        all_subdivisions={"A", "B"},
    )
    assert entry["regional"] is False


# ── Adoption respects regionality (PHASE 10) ─────────────────────────────────
def test_regional_holidays_are_excluded_from_adoption_by_default():
    from app.holidays.router import _eligibility
    regional = catalog.normalize_entry(
        {"name": "2 January", "date": "2026-01-02", "holiday_type": "Public",
         "regional": True, "regions": ["GB-SCT"]}, source="provider")
    reason = _eligibility(regional, include_optional=False, include_regional=False)
    assert reason is not None
    assert "GB-SCT" in reason, "the admin must be told WHERE it applies"


def test_regional_holidays_can_be_opted_into():
    from app.holidays.router import _eligibility
    regional = catalog.normalize_entry(
        {"name": "2 January", "date": "2026-01-02", "holiday_type": "Public",
         "regional": True, "regions": ["GB-SCT"]}, source="provider")
    assert _eligibility(regional, include_optional=False, include_regional=True) is None


def test_a_nationwide_public_holiday_is_adoptable_by_default():
    from app.holidays.router import _eligibility
    entry = catalog.normalize_entry(
        {"name": "New Year's Day", "date": "2026-01-01", "holiday_type": "Public"},
        source="provider")
    assert _eligibility(entry, include_optional=False, include_regional=False) is None


def test_optional_and_regional_gate_independently():
    from app.holidays.router import _eligibility
    optional_national = catalog.normalize_entry(
        {"name": "A", "date": "2026-01-01", "holiday_type": "Optional"}, source="provider")
    public_regional = catalog.normalize_entry(
        {"name": "B", "date": "2026-01-02", "holiday_type": "Public",
         "regional": True, "regions": ["X"]}, source="provider")

    assert _eligibility(optional_national, True, False) is None
    assert _eligibility(optional_national, False, True) is not None
    assert _eligibility(public_regional, False, True) is None
    assert _eligibility(public_regional, True, False) is not None
