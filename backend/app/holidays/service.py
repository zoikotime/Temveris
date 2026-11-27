"""
Temveris – Holiday catalog service
===================================
The one path from "a country code and a year" to "a normalized calendar".

    route ──► service ──► curated catalog  (India, UAE — provider has no data)
                     └──► Nager.Date provider (everything else)

Routes never touch ``provider`` or ``catalog`` directly, so the provider stays
swappable and the API contract stays stable. This layer owns:

  * ISO-2 / year validation,
  * choosing curated vs provider for a country,
  * the curated fallback when the provider fails on a country we also ship,
  * the server-side response cache (TTL'd, bounded, and holding only public
    reference data — never anything organization-private),
  * turning provider failures into ``CatalogError`` with a distinct ``kind``.

Why the cache lives here and not in the browser
-----------------------------------------------
The old client cached provider responses in ``localStorage`` under
``zt_holidays_{code}_{year}`` with no expiry and no scoping: a correction
upstream never reached a browser that had cached the old list, and two admins
from different organizations on one machine shared the entry. Caching here is
TTL'd, shared by every user, invalidatable, and — because a national holiday
calendar is public reference data, identical for every tenant — carries no
tenant-private information at all.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from app.holidays import catalog, provider

_log = logging.getLogger("zoikotime.holidays.service")

# A country's calendar for a past or current year is effectively immutable, but
# governments do amend upcoming years, so this is hours rather than forever.
CALENDAR_TTL_SECONDS = 6 * 60 * 60
# Bounded so a scripted sweep over every country/year cannot grow the process.
CALENDAR_CACHE_MAX_ENTRIES = 512

_cache_lock = threading.Lock()
_calendar_cache: Dict[Tuple[str, int], Tuple[float, Dict[str, Any]]] = {}


class CatalogError(Exception):
    """A calendar request that could not be served.

    ``kind`` drives both the HTTP status and the message the admin sees. The
    kinds are deliberately distinct because the audit found every failure —
    unsupported country, provider 500, DNS failure, empty result — collapsing
    into one "Could not load holidays for this country".
    """

    def __init__(self, kind: str, message: str, *, status_code: int = 502):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


KIND_INVALID_COUNTRY = "invalid_country"
KIND_INVALID_YEAR = "invalid_year"
KIND_UNSUPPORTED_COUNTRY = "unsupported_country"
KIND_PROVIDER_UNAVAILABLE = "provider_unavailable"

# Provider failure kind → (catalog kind, HTTP status). A country the provider
# genuinely has no data for is a 404 about the *country*, not a provider fault;
# everything else is a 502, because the request was fine and the upstream was not.
_PROVIDER_KIND_MAP: Dict[str, Tuple[str, int]] = {
    provider.KIND_UNSUPPORTED: (KIND_UNSUPPORTED_COUNTRY, 404),
    provider.KIND_TIMEOUT: (KIND_PROVIDER_UNAVAILABLE, 504),
    provider.KIND_NETWORK: (KIND_PROVIDER_UNAVAILABLE, 502),
    provider.KIND_PROVIDER_ERROR: (KIND_PROVIDER_UNAVAILABLE, 502),
    provider.KIND_MALFORMED: (KIND_PROVIDER_UNAVAILABLE, 502),
}


# ── Cache ─────────────────────────────────────────────────────────────────────
def _cache_get(key: Tuple[str, int]) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        hit = _calendar_cache.get(key)
        if hit is None:
            return None
        stored_at, payload = hit
        if (time.monotonic() - stored_at) >= CALENDAR_TTL_SECONDS:
            _calendar_cache.pop(key, None)
            return None
        return payload


def _cache_put(key: Tuple[str, int], payload: Dict[str, Any]) -> None:
    with _cache_lock:
        if len(_calendar_cache) >= CALENDAR_CACHE_MAX_ENTRIES:
            # Evict the oldest few rather than clearing everything, so a busy
            # workspace does not lose its whole warm set on one overflow.
            for stale_key in sorted(
                _calendar_cache, key=lambda k: _calendar_cache[k][0]
            )[: max(1, CALENDAR_CACHE_MAX_ENTRIES // 8)]:
                _calendar_cache.pop(stale_key, None)
        _calendar_cache[key] = (time.monotonic(), payload)


def invalidate_cache(country_code: Optional[str] = None, year: Optional[int] = None) -> int:
    """Drop cached calendars. Whole cache, one country, or one country+year."""
    cc = catalog.normalize_country_code(country_code) if country_code else None
    with _cache_lock:
        if cc is None:
            dropped = len(_calendar_cache)
            _calendar_cache.clear()
            return dropped
        keys = [
            k for k in _calendar_cache
            if k[0] == cc and (year is None or k[1] == int(year))
        ]
        for k in keys:
            _calendar_cache.pop(k, None)
        return len(keys)


# ── Country registry ──────────────────────────────────────────────────────────
def list_supported_countries() -> Dict[str, Any]:
    """Every country the admin may pick, curated entries merged over provider ones.

    ``support`` tells the UI which engine will serve the country:
      * ``curated``  – Temveris ships the calendar (India, UAE)
      * ``provider`` – Nager.Date covers it

    A curated country wins over a provider entry of the same code, so if
    Nager.Date ever adds India we keep serving the reconciled central list
    rather than silently switching sources mid-year.

    ``provider_available`` is False when the upstream country list could not be
    fetched; the curated countries are still returned, which is precisely the
    case the old browser-side code turned into an empty dropdown.
    """
    provider_countries = provider.available_countries()
    merged: Dict[str, Dict[str, Any]] = {}

    for entry in provider_countries:
        merged[entry["country_code"]] = {
            "country_code": entry["country_code"],
            "country_name": entry["country_name"] or entry["country_code"],
            "support": catalog.SUPPORT_PROVIDER,
        }

    for entry in catalog.curated_countries():
        merged[entry["country_code"]] = {
            "country_code": entry["country_code"],
            "country_name": entry["country_name"],
            "support": catalog.SUPPORT_CURATED,
        }

    countries = sorted(merged.values(), key=lambda c: c["country_name"])
    return {
        "countries": countries,
        "provider": provider.PROVIDER_NAME,
        "provider_available": bool(provider_countries),
        "curated_years": {"min": catalog.CURATED_MIN_YEAR, "max": catalog.CURATED_MAX_YEAR},
        "year_range": {"min": catalog.MIN_YEAR, "max": catalog.MAX_YEAR},
    }


def country_support(code: str) -> str:
    """``curated`` / ``provider`` / ``unsupported`` for one ISO-2 code."""
    cc = catalog.normalize_country_code(code)
    if cc is None:
        return catalog.SUPPORT_UNSUPPORTED
    if catalog.has_curated_calendar(cc):
        return catalog.SUPPORT_CURATED
    supported = provider.supports_country(cc)
    if supported is True:
        return catalog.SUPPORT_PROVIDER
    if supported is False:
        return catalog.SUPPORT_UNSUPPORTED
    # Country list unavailable — do not claim the country is unsupported.
    return catalog.SUPPORT_PROVIDER


# ── The calendar ──────────────────────────────────────────────────────────────
def get_country_calendar(code: Any, year: Any, *, use_cache: bool = True) -> Dict[str, Any]:
    """Normalized public-holiday calendar for a country + year.

    Response shape (identical whichever engine served it)::

        {
          "country_code": "IN", "country_name": "India", "year": 2026,
          "source": "curated" | "provider",
          "partial": false,          # curated country, year outside our window
          "provider": "Nager.Date",
          "cached": false,
          "holidays": [ {name, date, day, holiday_type, description,
                         estimated, source}, ... ]
        }

    Raises ``CatalogError`` for a bad code, a bad year, a country nobody covers,
    or a provider that could not be reached.
    """
    cc = catalog.normalize_country_code(code)
    if cc is None:
        raise CatalogError(
            KIND_INVALID_COUNTRY,
            "Country code must be a 2-letter ISO 3166-1 alpha-2 code.",
            status_code=400,
        )
    if not catalog.is_valid_year(year):
        raise CatalogError(
            KIND_INVALID_YEAR,
            f"Year must be between {catalog.MIN_YEAR} and {catalog.MAX_YEAR}.",
            status_code=400,
        )
    y = int(year)
    key = (cc, y)

    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return {**cached, "cached": True}

    curated = catalog.curated_calendar(cc, y)
    if curated is not None:
        payload = {**curated, "provider": None, "cached": False}
        _cache_put(key, payload)
        return dict(payload)

    try:
        holidays = provider.fetch_holidays(cc, y)
    except provider.ProviderError as exc:
        kind, status = _PROVIDER_KIND_MAP.get(
            exc.kind, (KIND_PROVIDER_UNAVAILABLE, 502)
        )
        _log.warning(
            "holidays.service provider failure country=%s year=%s kind=%s: %s",
            cc, y, exc.kind, exc.message,
        )
        raise CatalogError(kind, exc.message, status_code=status) from exc

    payload = {
        "country_code": cc,
        "country_name": _provider_country_name(cc),
        "year": y,
        "source": catalog.SUPPORT_PROVIDER,
        "partial": False,
        "provider": provider.PROVIDER_NAME,
        "cached": False,
        "holidays": holidays,
    }
    _cache_put(key, payload)
    return dict(payload)


def _provider_country_name(code: str) -> str:
    for entry in provider.available_countries():
        if entry["country_code"] == code:
            return entry["country_name"] or code
    return code


def holiday_dates_for(code: Any, year: Any) -> List[str]:
    """Just the YYYY-MM-DD strings. Convenience for seeding and for tests."""
    return [h["date"] for h in get_country_calendar(code, year)["holidays"]]
