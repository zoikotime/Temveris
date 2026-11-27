"""
Temveris – External public-holiday provider (Nager.Date)
=========================================================
The provider used to be called straight from the browser:

    AdminDashboard.jsx  ──►  https://date.nager.at/api/v3/PublicHolidays/{year}/{code}
    SettingsPage.jsx    ──►  https://date.nager.at/api/v3/AvailableCountries

Two copies of the call, two different cache strategies, no timeout, no retry, no
status-code handling, and every failure mode collapsed into one red string. It
also meant the feature was dead on any network that blocks the upstream host.

This module is the only place that talks to Nager.Date. It is called from
``app/holidays/service.py`` and never reached directly by a route, so the
provider stays an implementation detail rather than a public contract: swapping
it, or adding a second provider, does not touch the API or the UI.

Contract
--------
Success returns already-normalized entries (``catalog.normalize_entry``).
Failure raises ``ProviderError`` carrying a ``kind`` the API turns into a
distinct, actionable message. The kinds exist because the audit found the UI
could not tell "this country has no holidays" from "the provider is down":

    unsupported_country – provider has no data for this ISO-2 code (HTTP 404)
    provider_error      – provider answered, but with 5xx or nonsense
    timeout             – provider did not answer in time
    network_error       – DNS / TLS / connection refused
    malformed_response  – 200, but the body was not a holiday list
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

import requests

from app.holidays import catalog

_log = logging.getLogger("zoikotime.holidays.provider")

PROVIDER_NAME = "Nager.Date"
BASE_URL = "https://date.nager.at/api/v3"

# Deliberately short. This sits inside an admin page load; a slow upstream must
# degrade to a clear message, not hold a worker thread open.
CONNECT_TIMEOUT_SECONDS = 4.0
READ_TIMEOUT_SECONDS = 8.0
_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)

# Nager.Date's own vocabulary → the shared three. Its `types` array can hold
# several values; anything containing "Public" wins, otherwise the day is
# Optional so it never silently becomes a non-working day for payroll.
PROVIDER_TYPE_MAP = {
    "public": catalog.HOLIDAY_TYPE_PUBLIC,
    "bank": catalog.HOLIDAY_TYPE_PUBLIC,
    "optional": catalog.HOLIDAY_TYPE_OPTIONAL,
    "observance": catalog.HOLIDAY_TYPE_OPTIONAL,
    "school": catalog.HOLIDAY_TYPE_OPTIONAL,
    "authorities": catalog.HOLIDAY_TYPE_OPTIONAL,
}

KIND_UNSUPPORTED = "unsupported_country"
KIND_PROVIDER_ERROR = "provider_error"
KIND_TIMEOUT = "timeout"
KIND_NETWORK = "network_error"
KIND_MALFORMED = "malformed_response"


class ProviderError(Exception):
    """A provider call that could not produce a calendar.

    ``kind`` is one of the KIND_* constants above; the service layer maps it to
    an HTTP status and a message the admin can act on.
    """

    def __init__(self, kind: str, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_code = status_code


# ── Supported-country list ────────────────────────────────────────────────────
# The list changes rarely (it is a list of countries), so it is held in a
# process-local cache with a TTL rather than fetched per request. A failure
# serves the last good copy if we have one — a stale country list is far better
# than an empty country dropdown.
_COUNTRIES_TTL_SECONDS = 24 * 60 * 60
_countries_lock = threading.Lock()
_countries_cache: Dict[str, Any] = {"fetched_at": 0.0, "value": None}


def _get(path: str) -> requests.Response:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    try:
        return requests.get(url, timeout=_TIMEOUT, headers={"Accept": "application/json"})
    except requests.exceptions.Timeout as exc:
        raise ProviderError(
            KIND_TIMEOUT,
            f"{PROVIDER_NAME} did not respond within "
            f"{int(CONNECT_TIMEOUT_SECONDS + READ_TIMEOUT_SECONDS)}s.",
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise ProviderError(
            KIND_NETWORK, f"Could not reach {PROVIDER_NAME}."
        ) from exc


def available_countries(*, force: bool = False) -> List[Dict[str, str]]:
    """Countries Nager.Date can serve, as ``{country_code, country_name}``.

    Never raises: an upstream failure returns the cached copy, or an empty list
    when there is nothing cached. The caller merges the curated countries in, so
    India and the UAE remain selectable even with the provider unreachable —
    which is exactly the situation the old client-side code handled worst.
    """
    now = time.monotonic()
    with _countries_lock:
        cached = _countries_cache["value"]
        fresh = cached is not None and (now - _countries_cache["fetched_at"]) < _COUNTRIES_TTL_SECONDS
        if fresh and not force:
            return list(cached)

    try:
        response = _get("AvailableCountries")
        if response.status_code != 200:
            raise ProviderError(
                KIND_PROVIDER_ERROR,
                f"{PROVIDER_NAME} returned HTTP {response.status_code}.",
                status_code=response.status_code,
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise ProviderError(KIND_MALFORMED, f"{PROVIDER_NAME} returned an unexpected body.")
        countries = [
            {
                "country_code": str(item.get("countryCode") or "").strip().upper(),
                "country_name": str(item.get("name") or "").strip(),
            }
            for item in payload
            if isinstance(item, dict) and catalog.is_valid_country_code(item.get("countryCode"))
        ]
    except (ProviderError, ValueError) as exc:
        _log.warning("holidays.provider available_countries failed: %s", exc)
        with _countries_lock:
            return list(_countries_cache["value"] or [])

    with _countries_lock:
        _countries_cache["value"] = countries
        _countries_cache["fetched_at"] = time.monotonic()
    return list(countries)


def supports_country(code: str) -> Optional[bool]:
    """True/False when the provider list is known, None when it is not.

    ``None`` matters: with the country list unavailable we must not claim a
    country is unsupported. The service treats None as "try the call and find
    out" so a transient list failure never mislabels a real country.
    """
    countries = available_countries()
    if not countries:
        return None
    cc = catalog.normalize_country_code(code)
    return any(c["country_code"] == cc for c in countries)


def _normalize_provider_holiday(
    item: Dict[str, Any],
    *,
    all_subdivisions: Optional[set] = None,
) -> Optional[Dict[str, Any]]:
    """One provider row → the shared normalized shape.

    ``all_subdivisions`` is the union of every ``counties`` value seen across
    the country's whole year. It is how we tell a genuinely regional holiday
    from one that merely ENUMERATES every subdivision:

        GB New Year's Day  counties = [GB-ENG, GB-NIR, GB-SCT, GB-WLS]  -> nationwide
        GB "2 January"     counties = [GB-SCT]                          -> regional

    Deriving it from the payload means no hardcoded subdivision tables to keep
    up to date, and it degrades safely: with the union unknown, any non-empty
    ``counties`` is treated as regional, which is the previous behaviour.
    """
    date_str = str(item.get("date") or "").strip()
    if not date_str:
        return None

    # holiday_type now reflects ONLY the provider's own type vocabulary.
    # Regionality is carried separately — see catalog.normalize_entry.
    types = item.get("types")
    holiday_type = catalog.HOLIDAY_TYPE_PUBLIC
    if isinstance(types, list) and types:
        mapped = [PROVIDER_TYPE_MAP.get(str(t).strip().lower()) for t in types]
        holiday_type = (
            catalog.HOLIDAY_TYPE_PUBLIC
            if catalog.HOLIDAY_TYPE_PUBLIC in mapped
            else catalog.HOLIDAY_TYPE_OPTIONAL
        )

    local_name = str(item.get("localName") or "").strip()
    english_name = str(item.get("name") or "").strip()
    # Prefer the local name in the title and keep the English one as context.
    name = local_name or english_name
    description = english_name if english_name and english_name != name else ""

    counties = [str(c).strip() for c in (item.get("counties") or []) if str(c).strip()]
    if not counties:
        regional, regions = False, []
    elif all_subdivisions and set(counties) >= set(all_subdivisions):
        # Enumerates every subdivision — nationwide, just spelled out.
        regional, regions = False, []
    else:
        regional, regions = True, counties

    return catalog.normalize_entry(
        {
            "name": name,
            "date": date_str,
            "holiday_type": holiday_type,
            "description": description,
            "estimated": False,
            "regional": regional,
            "regions": regions,
        },
        source=catalog.SUPPORT_PROVIDER,
    )


def fetch_holidays(code: str, year: int) -> List[Dict[str, Any]]:
    """Normalized holidays for a provider-supported country + year.

    Raises ``ProviderError`` on every failure path — the caller decides whether
    to fall back to a curated calendar or surface the error.
    """
    cc = catalog.normalize_country_code(code)
    if cc is None:
        raise ProviderError(KIND_UNSUPPORTED, "Country code must be a 2-letter ISO code.")

    response = _get(f"PublicHolidays/{int(year)}/{cc}")

    if response.status_code == 404:
        raise ProviderError(
            KIND_UNSUPPORTED,
            f"{PROVIDER_NAME} has no calendar for this country.",
            status_code=404,
        )
    if response.status_code >= 500:
        raise ProviderError(
            KIND_PROVIDER_ERROR,
            f"{PROVIDER_NAME} returned HTTP {response.status_code}.",
            status_code=response.status_code,
        )
    if response.status_code != 200:
        raise ProviderError(
            KIND_PROVIDER_ERROR,
            f"{PROVIDER_NAME} returned HTTP {response.status_code}.",
            status_code=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise ProviderError(
            KIND_MALFORMED, f"{PROVIDER_NAME} returned a body that is not JSON."
        ) from exc

    if not isinstance(payload, list):
        raise ProviderError(
            KIND_MALFORMED, f"{PROVIDER_NAME} returned an unexpected body."
        )

    # First pass: the union of every subdivision the country mentions all year.
    # An entry listing all of them is nationwide, not regional.
    all_subdivisions = {
        str(c).strip()
        for item in payload if isinstance(item, dict)
        for c in (item.get("counties") or []) if str(c).strip()
    }

    holidays = [
        normalized
        for normalized in (
            _normalize_provider_holiday(item, all_subdivisions=all_subdivisions)
            for item in payload if isinstance(item, dict)
        )
        if normalized is not None
    ]
    holidays.sort(key=lambda h: (h["date"], h["name"]))
    return holidays


def reset_caches() -> None:
    """Drop the process-local country cache. Used by tests."""
    with _countries_lock:
        _countries_cache["value"] = None
        _countries_cache["fetched_at"] = 0.0
