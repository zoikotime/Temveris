"""
Holiday catalog, provider and service — pure unit tests (no DB, no network).

The provider is monkeypatched throughout: these tests must pass on a machine
with no internet, and must not depend on what Nager.Date happens to return
today. The one thing we DO assert about the real provider is structural — that
India and the UAE are treated as curated, which is the whole reason the curated
layer exists.
"""
from __future__ import annotations

import pytest

from app.holidays import catalog, provider, service


@pytest.fixture(autouse=True)
def _clean_caches():
    provider.reset_caches()
    service.invalidate_cache()
    yield
    provider.reset_caches()
    service.invalidate_cache()


# ── Type vocabulary ───────────────────────────────────────────────────────────
def test_holiday_types_are_the_three_shared_values():
    assert catalog.HOLIDAY_TYPES == ("Public", "Company", "Optional")


@pytest.mark.parametrize("raw,expected", [
    ("Public", "Public"), ("public", "Public"), ("PUBLIC", "Public"),
    ("Company", "Company"), ("Optional", "Optional"),
    ("Bank", "Public"), ("National holiday", "Public"),
    ("Observance", "Optional"), ("School", "Optional"), ("Authorities", "Optional"),
    ("", "Public"), (None, "Public"),
])
def test_normalize_holiday_type(raw, expected):
    assert catalog.normalize_holiday_type(raw) == expected


def test_unknown_type_becomes_optional_not_public():
    """Over-reporting a day as Public marks it non-working for attendance and
    payroll. An unrecognised label must fail toward the harmless value."""
    assert catalog.normalize_holiday_type("Festival of Unknown Origin") == "Optional"


# ── Country / year validation ─────────────────────────────────────────────────
@pytest.mark.parametrize("code,ok", [
    ("IN", True), ("in", True), (" us ", True), ("GB", True),
    ("IND", False), ("I", False), ("", False), (None, False),
    ("1N", False), ("!!", False),
])
def test_country_code_validation(code, ok):
    assert catalog.is_valid_country_code(code) is ok
    assert (catalog.normalize_country_code(code) is not None) is ok


def test_normalize_country_code_uppercases():
    assert catalog.normalize_country_code(" in ") == "IN"


@pytest.mark.parametrize("year,ok", [
    (2026, True), (2000, True), (2100, True),
    (1999, False), (2101, False), ("nope", False), (None, False),
])
def test_year_validation(year, ok):
    assert catalog.is_valid_year(year) is ok


# ── Curated calendars ─────────────────────────────────────────────────────────
def test_india_and_uae_are_curated():
    """The two countries Nager.Date does not cover. If this ever fails, the
    Manage Holidays page is back to showing 'Could not load holidays'."""
    assert catalog.has_curated_calendar("IN")
    assert catalog.has_curated_calendar("AE")


def test_curated_countries_listed_alphabetically():
    names = [c["country_name"] for c in catalog.curated_countries()]
    assert names == sorted(names)
    assert {"India", "United Arab Emirates"} <= set(names)


def test_india_2026_resolves_the_documented_date_disputes():
    """The two dates the shipped calendars disagreed on, pinned so a future edit
    has to be deliberate. See the catalog module docstring for the reasoning."""
    holidays = {h["name"]: h["date"] for h in catalog.curated_calendar("IN", 2026)["holidays"]}
    assert holidays["Holi"] == "2026-03-04", "gazetted Dhulandi, not Holika Dahan on 03-03"
    assert holidays["Eid-ul-Adha (Bakrid)"] == "2026-05-27"


def test_india_2026_is_the_union_not_the_shorter_seed_list():
    """router.py shipped 13 entries, SettingsPage shipped 21. The reconciled
    calendar keeps the fuller list plus Ganesh Chaturthi."""
    holidays = catalog.curated_calendar("IN", 2026)["holidays"]
    names = {h["name"] for h in holidays}
    assert len(holidays) >= 21
    # Only in the old SettingsPage list.
    assert {"Makar Sankranti / Pongal", "Ram Navami", "Guru Nanak Jayanti",
            "Milad-un-Nabi", "Muharram"} <= names
    # Only in the old router.py list.
    assert "Ganesh Chaturthi" in names


def test_ganesh_chaturthi_is_optional_not_public():
    """Restricted centrally. Importing it as Public would mark a working day
    non-working for every employee."""
    holidays = {h["name"]: h for h in catalog.curated_calendar("IN", 2026)["holidays"]}
    assert holidays["Ganesh Chaturthi"]["holiday_type"] == "Optional"


def test_gazetted_national_days_are_public():
    holidays = {h["name"]: h for h in catalog.curated_calendar("IN", 2026)["holidays"]}
    for name in ("Republic Day", "Independence Day", "Gandhi Jayanti"):
        assert holidays[name]["holiday_type"] == "Public", name


def test_lunar_holidays_are_flagged_estimated():
    """Moon-sighting dates can move. We say so instead of presenting them as
    settled — the alternative would be fabricating certainty."""
    holidays = {h["name"]: h for h in catalog.curated_calendar("IN", 2026)["holidays"]}
    assert holidays["Eid-ul-Fitr"]["estimated"] is True
    assert holidays["Eid-ul-Adha (Bakrid)"]["estimated"] is True
    assert holidays["Republic Day"]["estimated"] is False


def test_two_holidays_may_share_a_date():
    """Gandhi Jayanti and Dussehra genuinely coincided on 2025-10-02. The old
    date-only uniqueness rule made this unstorable."""
    dates = [h["date"] for h in catalog.curated_calendar("IN", 2025)["holidays"]]
    assert dates.count("2025-10-02") == 2


def test_uae_2026_covers_fixed_and_lunar_days():
    payload = catalog.curated_calendar("AE", 2026)
    by_name = {h["name"]: h for h in payload["holidays"]}
    assert by_name["National Day"]["date"] == "2026-12-02"
    assert by_name["Commemoration Day"]["date"] == "2026-12-01"
    assert by_name["Commemoration Day"]["estimated"] is False
    assert by_name["Eid al Fitr"]["estimated"] is True


@pytest.mark.parametrize("code", ["IN", "AE"])
@pytest.mark.parametrize("year", [2024, 2025, 2026, 2027])
def test_curated_years_are_internally_consistent(code, year):
    payload = catalog.curated_calendar(code, year)
    assert payload["partial"] is False
    assert payload["holidays"], f"{code} {year} is empty"
    for entry in payload["holidays"]:
        assert entry["date"].startswith(str(year)), entry
        assert entry["name"]
        assert entry["holiday_type"] in catalog.HOLIDAY_TYPES
        assert entry["day"], "weekday must be derived"
        assert entry["source"] == "curated"


def test_year_outside_curated_window_returns_fixed_dates_marked_partial():
    payload = catalog.curated_calendar("IN", 2035)
    assert payload["partial"] is True
    dates = {h["date"] for h in payload["holidays"]}
    assert "2035-01-26" in dates   # Republic Day is 26 January in every year
    assert "2035-08-15" in dates


def test_curated_calendar_none_for_unknown_country():
    assert catalog.curated_calendar("FR", 2026) is None
    assert catalog.curated_calendar("ZZZ", 2026) is None


def test_weekday_name():
    assert catalog.weekday_name("2026-01-26") == "Monday"
    assert catalog.weekday_name("garbage") == ""


# ── Provider normalization ────────────────────────────────────────────────────
def test_provider_entry_prefers_local_name_and_keeps_english_as_description():
    entry = provider._normalize_provider_holiday({
        "date": "2026-01-01", "localName": "Neujahr", "name": "New Year's Day",
        "types": ["Public"], "counties": None,
    })
    assert entry["name"] == "Neujahr"
    assert entry["description"] == "New Year's Day"
    assert entry["holiday_type"] == "Public"
    assert entry["source"] == "provider"
    assert entry["day"] == "Thursday"


def test_provider_regional_holiday_is_flagged_not_retyped():
    """A holiday observed in two states is regional — but it is still a PUBLIC
    holiday where it applies.

    This assertion used to read ``holiday_type == "Optional"``. Encoding
    regionality in the type destroyed information both ways: UK New Year's Day
    (which lists every constituent country, so it is nationwide) was downgraded
    and skipped on adoption, while India's genuinely-Optional New Year's Day
    was rendered as "Regional". The two facts now have two fields — see
    tests/test_holidays_regional.py.
    """
    entry = provider._normalize_provider_holiday(
        {"date": "2026-06-08", "localName": "Corpus Christi", "name": "Corpus Christi",
         "types": ["Public"], "counties": ["DE-BW", "DE-BY"]},
        all_subdivisions={"DE-BW", "DE-BY", "DE-NW", "DE-SL"},
    )
    assert entry["holiday_type"] == "Public"
    assert entry["regional"] is True
    assert entry["regions"] == ["DE-BW", "DE-BY"]


def test_provider_observance_becomes_optional():
    entry = provider._normalize_provider_holiday({
        "date": "2026-02-14", "localName": "Valentine's Day", "name": "Valentine's Day",
        "types": ["Observance"],
    })
    assert entry["holiday_type"] == "Optional"


# ── Service: routing between curated and provider ─────────────────────────────
def _fake_countries(monkeypatch, codes):
    monkeypatch.setattr(
        provider, "available_countries",
        lambda **kw: [{"country_code": c, "country_name": c} for c in codes],
    )


def test_india_never_calls_the_provider(monkeypatch):
    """The original bug: India went to Nager.Date, which 404s, and the UI showed
    'Could not load holidays for this country'."""
    def _explode(*a, **k):
        raise AssertionError("provider must not be called for a curated country")
    monkeypatch.setattr(provider, "fetch_holidays", _explode)

    payload = service.get_country_calendar("IN", 2026)
    assert payload["source"] == "curated"
    assert payload["country_name"] == "India"
    assert len(payload["holidays"]) >= 21


def test_uae_never_calls_the_provider(monkeypatch):
    monkeypatch.setattr(provider, "fetch_holidays",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")))
    payload = service.get_country_calendar("AE", 2026)
    assert payload["source"] == "curated"
    assert payload["holidays"]


def test_provider_backed_country_uses_the_provider(monkeypatch):
    _fake_countries(monkeypatch, ["US"])
    monkeypatch.setattr(provider, "fetch_holidays", lambda code, year: [
        catalog.normalize_entry(
            {"name": "Independence Day", "date": f"{year}-07-04",
             "holiday_type": "Public"}, source="provider")
    ])
    payload = service.get_country_calendar("US", 2026)
    assert payload["source"] == "provider"
    assert payload["holidays"][0]["date"] == "2026-07-04"


def test_invalid_country_is_a_400_not_a_provider_call():
    with pytest.raises(service.CatalogError) as exc:
        service.get_country_calendar("USA", 2026)
    assert exc.value.kind == service.KIND_INVALID_COUNTRY
    assert exc.value.status_code == 400


def test_invalid_year_is_a_400():
    with pytest.raises(service.CatalogError) as exc:
        service.get_country_calendar("US", 12026)
    assert exc.value.kind == service.KIND_INVALID_YEAR
    assert exc.value.status_code == 400


@pytest.mark.parametrize("provider_kind,expected_kind,expected_status", [
    (provider.KIND_UNSUPPORTED, service.KIND_UNSUPPORTED_COUNTRY, 404),
    (provider.KIND_TIMEOUT, service.KIND_PROVIDER_UNAVAILABLE, 504),
    (provider.KIND_NETWORK, service.KIND_PROVIDER_UNAVAILABLE, 502),
    (provider.KIND_PROVIDER_ERROR, service.KIND_PROVIDER_UNAVAILABLE, 502),
    (provider.KIND_MALFORMED, service.KIND_PROVIDER_UNAVAILABLE, 502),
])
def test_provider_failures_keep_their_identity(monkeypatch, provider_kind,
                                               expected_kind, expected_status):
    """Every failure used to collapse into one message. They must stay distinct
    so the admin is told whether to pick a different country or try again."""
    _fake_countries(monkeypatch, ["FR"])

    def _fail(code, year):
        raise provider.ProviderError(provider_kind, "upstream said no")
    monkeypatch.setattr(provider, "fetch_holidays", _fail)

    with pytest.raises(service.CatalogError) as exc:
        service.get_country_calendar("FR", 2026)
    assert exc.value.kind == expected_kind
    assert exc.value.status_code == expected_status


def test_empty_provider_result_is_success_not_an_error(monkeypatch):
    """A country with genuinely no holidays that year is an empty list, which
    the UI renders as an empty state — not as a failure."""
    _fake_countries(monkeypatch, ["XX"])
    monkeypatch.setattr(provider, "fetch_holidays", lambda code, year: [])
    payload = service.get_country_calendar("XX", 2026)
    assert payload["holidays"] == []
    assert payload["source"] == "provider"


# ── Service: caching ──────────────────────────────────────────────────────────
def test_provider_result_is_cached_and_invalidatable(monkeypatch):
    _fake_countries(monkeypatch, ["US"])
    calls = []

    def _count(code, year):
        calls.append((code, year))
        return []
    monkeypatch.setattr(provider, "fetch_holidays", _count)

    first = service.get_country_calendar("US", 2026)
    second = service.get_country_calendar("US", 2026)
    assert len(calls) == 1, "second read must come from the cache"
    assert first["cached"] is False and second["cached"] is True

    service.invalidate_cache("US", 2026)
    service.get_country_calendar("US", 2026)
    assert len(calls) == 2


def test_cache_is_keyed_by_country_and_year(monkeypatch):
    _fake_countries(monkeypatch, ["US", "GB"])
    monkeypatch.setattr(provider, "fetch_holidays", lambda code, year: [
        catalog.normalize_entry({"name": f"{code}-{year}", "date": f"{year}-01-01"},
                                source="provider")])
    assert service.get_country_calendar("US", 2026)["holidays"][0]["name"] == "US-2026"
    assert service.get_country_calendar("GB", 2026)["holidays"][0]["name"] == "GB-2026"
    assert service.get_country_calendar("US", 2027)["holidays"][0]["name"] == "US-2027"


def test_cache_is_bounded(monkeypatch):
    _fake_countries(monkeypatch, [])
    monkeypatch.setattr(provider, "fetch_holidays", lambda code, year: [])
    for i in range(service.CALENDAR_CACHE_MAX_ENTRIES + 40):
        service.get_country_calendar("US", 2000 + (i % 100))
    assert len(service._calendar_cache) <= service.CALENDAR_CACHE_MAX_ENTRIES


# ── Service: country registry ─────────────────────────────────────────────────
def test_curated_countries_survive_a_dead_provider(monkeypatch):
    """The old client-side code produced an EMPTY dropdown when the provider was
    unreachable. India and the UAE must always be pickable."""
    monkeypatch.setattr(provider, "available_countries", lambda **kw: [])
    payload = service.list_supported_countries()
    codes = {c["country_code"] for c in payload["countries"]}
    assert {"IN", "AE"} <= codes
    assert payload["provider_available"] is False


def test_curated_wins_over_a_provider_entry_of_the_same_code(monkeypatch):
    _fake_countries(monkeypatch, ["IN", "US"])
    payload = service.list_supported_countries()
    india = next(c for c in payload["countries"] if c["country_code"] == "IN")
    assert india["support"] == "curated"
    assert india["country_name"] == "India"


def test_countries_are_sorted_by_name(monkeypatch):
    _fake_countries(monkeypatch, ["US", "AU", "CA"])
    names = [c["country_name"] for c in service.list_supported_countries()["countries"]]
    assert names == sorted(names)


def test_country_support_classification(monkeypatch):
    _fake_countries(monkeypatch, ["US", "GB"])
    assert service.country_support("IN") == "curated"
    assert service.country_support("AE") == "curated"
    assert service.country_support("US") == "provider"
    assert service.country_support("ZZ") == "unsupported"
    assert service.country_support("bad") == "unsupported"


def test_unknown_support_is_not_reported_as_unsupported(monkeypatch):
    """With the provider list unavailable we must not claim a real country is
    unsupported — that would block a legitimate choice on a transient failure."""
    monkeypatch.setattr(provider, "available_countries", lambda **kw: [])
    assert service.country_support("FR") == "provider"
