"""
Temveris – Authoritative public-holiday catalog
================================================
ONE source of truth for:

  * the shared holiday **type vocabulary** (Public / Company / Optional),
  * which **countries** Temveris can produce a calendar for and *how*,
  * the **curated calendars** for countries the external provider does not cover,
  * the **normalized holiday shape** every surface renders.

Before this module the same information lived in three places that disagreed
with each other:

  * ``app/holidays/router.py``   – a 13-entry Indian 2026 calendar used to seed
                                   new organizations.
  * ``SettingsPage.jsx``         – a 21-entry Indian calendar for 2024-2027,
                                   with *different dates* for Holi and Bakrid.
  * ``AdminDashboard.jsx``       – no Indian calendar at all, which is why
                                   picking "India" showed
                                   "Could not load holidays for this country".

Everything now reads from here. The frontend reaches it over
``GET /holidays/countries`` and ``GET /holidays/catalog`` rather than keeping a
copy, so the calendars cannot drift apart again.

Reconciling the two Indian calendars
------------------------------------
The two shipped lists disagreed on two dates and on which festivals belong on
the list at all. Resolved deliberately, and recorded here rather than in a
commit message so the next person can audit the choice:

===========================  ==============  ==============  ==================
Holiday                      router.py       SettingsPage    Adopted
===========================  ==============  ==============  ==================
Holi                         2026-03-04      2026-03-03      **2026-03-04**
Eid-ul-Adha (Bakrid)         2026-05-28      2026-05-27      **2026-05-27**
Ganesh Chaturthi             2026-09-14      (absent)        **2026-09-14**
9 further festivals          (absent)        present         **present**
===========================  ==============  ==============  ==================

* **Holi** — Phalguna Purnima falls on 3 March 2026, which is *Holika Dahan*.
  The Government of India's gazetted "Holi" is the following day, Dhulandi /
  Rangwali Holi. ``router.py`` had the gazetted day; SettingsPage had the
  bonfire the evening before. Adopted 2026-03-04.
* **Eid-ul-Adha** — 10 Dhu al-Hijjah 1447. Published civil estimates cluster on
  27 May 2026; 28 May was the outlier. Adopted 2026-05-27, and flagged
  ``estimated`` because the date is fixed by moon sighting and can move a day.
* **Ganesh Chaturthi** — genuinely on 14 September 2026, but it is a *restricted*
  holiday centrally rather than a gazetted one, so it is carried as ``Optional``
  rather than dropped or promoted.
* The 9 festivals only SettingsPage had are real central-list entries. The
  shorter list was a seed subset, not a correction.

Every lunar / moon-sighting-dependent entry carries ``estimated=True``. Callers
surface that so an admin knows to confirm the date against their local
notification instead of trusting us. We do not invent dates we cannot support:
a country with no provider coverage and no curated calendar is reported as
``unsupported`` rather than given a plausible-looking list.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# ── Shared type vocabulary ────────────────────────────────────────────────────
# The DB column, the API, the CSV importer and both admin UIs all use exactly
# these three. Provider vocabularies are mapped onto them (see PROVIDER_TYPE_MAP
# in provider.py) so one chip palette covers every source.
HOLIDAY_TYPE_PUBLIC = "Public"
HOLIDAY_TYPE_COMPANY = "Company"
HOLIDAY_TYPE_OPTIONAL = "Optional"

HOLIDAY_TYPES: Tuple[str, ...] = (
    HOLIDAY_TYPE_PUBLIC,
    HOLIDAY_TYPE_COMPANY,
    HOLIDAY_TYPE_OPTIONAL,
)

DEFAULT_HOLIDAY_TYPE = HOLIDAY_TYPE_PUBLIC


def normalize_holiday_type(raw: Any) -> str:
    """Coerce any inbound type string onto the shared vocabulary.

    Case-insensitive, and tolerant of the richer vocabularies that leaked in
    from Nager.Date ("Bank", "Observance", "Authorities", "School") and from
    the old SettingsPage UI. Anything unrecognised becomes ``Optional`` rather
    than ``Public`` — over-reporting a day as a full public holiday would mark
    it non-working for attendance and payroll, so the safe default is the one
    that does not.
    """
    text = str(raw or "").strip()
    if not text:
        return DEFAULT_HOLIDAY_TYPE
    lowered = text.lower()
    for known in HOLIDAY_TYPES:
        if lowered == known.lower():
            return known
    if "public" in lowered or "national" in lowered or "bank" in lowered:
        return HOLIDAY_TYPE_PUBLIC
    if "company" in lowered or "org" in lowered:
        return HOLIDAY_TYPE_COMPANY
    return HOLIDAY_TYPE_OPTIONAL


# ── Country support model ─────────────────────────────────────────────────────
SUPPORT_CURATED = "curated"        # we ship the calendar ourselves
SUPPORT_PROVIDER = "provider"      # Nager.Date covers it
SUPPORT_UNSUPPORTED = "unsupported"

# Years a curated calendar is expected to cover. Outside this the curated
# countries fall back to their fixed-date subset (see `_fixed_only`), which is
# always correct but incomplete — flagged as `partial` in the response.
CURATED_MIN_YEAR = 2024
CURATED_MAX_YEAR = 2027

# The overall year window the API will accept at all. Wide enough for planning
# a couple of years out, narrow enough that a typo like `year=20026` is a clean
# 400 instead of a pointless upstream round-trip.
MIN_YEAR = 2000
MAX_YEAR = 2100


def _h(
    date_str: str,
    name: str,
    holiday_type: str = HOLIDAY_TYPE_PUBLIC,
    *,
    estimated: bool = False,
    description: str = "",
) -> Dict[str, Any]:
    """One curated calendar entry, in storage order (date first)."""
    return {
        "date": date_str,
        "name": name,
        "holiday_type": holiday_type,
        "estimated": estimated,
        "description": description,
    }


_MOON = "Date is set by moon sighting and may shift by a day."

# ── India ─────────────────────────────────────────────────────────────────────
# Central-government list. Nager.Date does not cover India at all — it is absent
# from its /AvailableCountries response — which is the original reason this
# module exists.
_INDIA: Dict[int, List[Dict[str, Any]]] = {
    2024: [
        _h("2024-01-01", "New Year's Day", HOLIDAY_TYPE_OPTIONAL),
        _h("2024-01-15", "Makar Sankranti / Pongal"),
        _h("2024-01-26", "Republic Day"),
        _h("2024-03-08", "Maha Shivratri"),
        _h("2024-03-25", "Holi"),
        _h("2024-03-29", "Good Friday"),
        _h("2024-04-11", "Eid-ul-Fitr", estimated=True, description=_MOON),
        _h("2024-04-14", "Ambedkar Jayanti"),
        _h("2024-04-17", "Ram Navami"),
        _h("2024-04-21", "Mahavir Jayanti"),
        _h("2024-05-01", "Labour Day"),
        _h("2024-05-23", "Buddha Purnima"),
        _h("2024-06-17", "Eid-ul-Adha (Bakrid)", estimated=True, description=_MOON),
        _h("2024-07-17", "Muharram", estimated=True, description=_MOON),
        _h("2024-08-15", "Independence Day"),
        _h("2024-08-26", "Janmashtami"),
        _h("2024-09-07", "Ganesh Chaturthi", HOLIDAY_TYPE_OPTIONAL,
           description="Restricted holiday centrally; a full public holiday in several states."),
        _h("2024-09-16", "Milad-un-Nabi", estimated=True, description=_MOON),
        _h("2024-10-02", "Gandhi Jayanti"),
        _h("2024-10-12", "Dussehra"),
        _h("2024-11-01", "Diwali"),
        _h("2024-11-15", "Guru Nanak Jayanti"),
        _h("2024-12-25", "Christmas"),
    ],
    2025: [
        _h("2025-01-01", "New Year's Day", HOLIDAY_TYPE_OPTIONAL),
        _h("2025-01-14", "Makar Sankranti / Pongal"),
        _h("2025-01-26", "Republic Day"),
        _h("2025-02-26", "Maha Shivratri"),
        _h("2025-03-14", "Holi"),
        _h("2025-03-31", "Eid-ul-Fitr", estimated=True, description=_MOON),
        _h("2025-04-06", "Ram Navami"),
        _h("2025-04-10", "Mahavir Jayanti"),
        _h("2025-04-14", "Ambedkar Jayanti"),
        _h("2025-04-18", "Good Friday"),
        _h("2025-05-01", "Labour Day"),
        _h("2025-05-12", "Buddha Purnima"),
        _h("2025-06-07", "Eid-ul-Adha (Bakrid)", estimated=True, description=_MOON),
        _h("2025-07-06", "Muharram", estimated=True, description=_MOON),
        _h("2025-08-15", "Independence Day"),
        _h("2025-08-16", "Janmashtami"),
        _h("2025-08-27", "Ganesh Chaturthi", HOLIDAY_TYPE_OPTIONAL,
           description="Restricted holiday centrally; a full public holiday in several states."),
        _h("2025-09-05", "Milad-un-Nabi", estimated=True, description=_MOON),
        # Gandhi Jayanti and Dussehra genuinely coincided on 2 October 2025.
        # Two holidays on one date is legitimate, which is why the DB uniqueness
        # rule includes `name` and not date alone.
        _h("2025-10-02", "Gandhi Jayanti"),
        _h("2025-10-02", "Dussehra"),
        _h("2025-10-20", "Diwali"),
        _h("2025-11-05", "Guru Nanak Jayanti"),
        _h("2025-12-25", "Christmas"),
    ],
    2026: [
        _h("2026-01-01", "New Year's Day", HOLIDAY_TYPE_OPTIONAL),
        _h("2026-01-14", "Makar Sankranti / Pongal"),
        _h("2026-01-26", "Republic Day"),
        _h("2026-02-15", "Maha Shivratri"),
        # Adopted from router.py — the gazetted day (Dhulandi), not Holika Dahan
        # on 3 March. See the module docstring.
        _h("2026-03-04", "Holi"),
        _h("2026-03-21", "Eid-ul-Fitr", estimated=True, description=_MOON),
        _h("2026-03-26", "Ram Navami"),
        _h("2026-04-01", "Mahavir Jayanti"),
        _h("2026-04-03", "Good Friday"),
        _h("2026-04-14", "Ambedkar Jayanti"),
        _h("2026-05-01", "Labour Day / Buddha Purnima"),
        # Adopted from SettingsPage — 27 May is the consensus civil estimate.
        _h("2026-05-27", "Eid-ul-Adha (Bakrid)", estimated=True, description=_MOON),
        _h("2026-06-26", "Muharram", estimated=True, description=_MOON),
        _h("2026-08-15", "Independence Day"),
        _h("2026-09-04", "Janmashtami"),
        _h("2026-09-14", "Ganesh Chaturthi", HOLIDAY_TYPE_OPTIONAL,
           description="Restricted holiday centrally; a full public holiday in several states."),
        _h("2026-09-24", "Milad-un-Nabi", estimated=True, description=_MOON),
        _h("2026-10-02", "Gandhi Jayanti"),
        _h("2026-10-20", "Dussehra"),
        _h("2026-11-08", "Diwali"),
        _h("2026-11-24", "Guru Nanak Jayanti"),
        _h("2026-12-25", "Christmas"),
    ],
    2027: [
        _h("2027-01-01", "New Year's Day", HOLIDAY_TYPE_OPTIONAL),
        _h("2027-01-14", "Makar Sankranti / Pongal"),
        _h("2027-01-26", "Republic Day"),
        _h("2027-03-06", "Maha Shivratri"),
        _h("2027-03-22", "Holi"),
        _h("2027-03-25", "Good Friday"),
        _h("2027-04-14", "Ambedkar Jayanti"),
        _h("2027-04-15", "Ram Navami"),
        _h("2027-05-01", "Labour Day"),
        _h("2027-05-20", "Buddha Purnima"),
        _h("2027-08-15", "Independence Day"),
        _h("2027-08-25", "Janmashtami"),
        _h("2027-10-02", "Gandhi Jayanti"),
        _h("2027-10-09", "Dussehra"),
        _h("2027-10-29", "Diwali"),
        _h("2027-11-14", "Guru Nanak Jayanti"),
        _h("2027-12-25", "Christmas"),
    ],
}

# ── United Arab Emirates ──────────────────────────────────────────────────────
# Also absent from Nager.Date. The UAE cabinet confirms the Islamic dates only
# weeks ahead, so every lunar entry here is `estimated` by construction — the
# API and UI say so rather than presenting them as settled.
_UAE: Dict[int, List[Dict[str, Any]]] = {
    2024: [
        _h("2024-01-01", "New Year's Day"),
        _h("2024-04-08", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2024-04-09", "Eid al Fitr", estimated=True, description=_MOON),
        _h("2024-04-10", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2024-04-11", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2024-06-15", "Arafat Day", estimated=True, description=_MOON),
        _h("2024-06-16", "Eid al Adha", estimated=True, description=_MOON),
        _h("2024-06-17", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2024-06-18", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2024-07-07", "Islamic New Year", estimated=True, description=_MOON),
        _h("2024-09-15", "Prophet Muhammad's Birthday", estimated=True, description=_MOON),
        _h("2024-12-01", "Commemoration Day"),
        _h("2024-12-02", "National Day"),
        _h("2024-12-03", "National Day Holiday"),
    ],
    2025: [
        _h("2025-01-01", "New Year's Day"),
        _h("2025-03-30", "Eid al Fitr", estimated=True, description=_MOON),
        _h("2025-03-31", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2025-04-01", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2025-06-05", "Arafat Day", estimated=True, description=_MOON),
        _h("2025-06-06", "Eid al Adha", estimated=True, description=_MOON),
        _h("2025-06-07", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2025-06-08", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2025-06-26", "Islamic New Year", estimated=True, description=_MOON),
        _h("2025-09-04", "Prophet Muhammad's Birthday", estimated=True, description=_MOON),
        _h("2025-12-01", "Commemoration Day"),
        _h("2025-12-02", "National Day"),
        _h("2025-12-03", "National Day Holiday"),
    ],
    2026: [
        _h("2026-01-01", "New Year's Day"),
        _h("2026-03-19", "Eid al Fitr", estimated=True, description=_MOON),
        _h("2026-03-20", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2026-03-21", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2026-05-26", "Arafat Day", estimated=True, description=_MOON),
        _h("2026-05-27", "Eid al Adha", estimated=True, description=_MOON),
        _h("2026-05-28", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2026-05-29", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2026-06-17", "Islamic New Year", estimated=True, description=_MOON),
        _h("2026-08-26", "Prophet Muhammad's Birthday", estimated=True, description=_MOON),
        _h("2026-12-01", "Commemoration Day"),
        _h("2026-12-02", "National Day"),
        _h("2026-12-03", "National Day Holiday"),
    ],
    2027: [
        _h("2027-01-01", "New Year's Day"),
        _h("2027-03-09", "Eid al Fitr", estimated=True, description=_MOON),
        _h("2027-03-10", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2027-03-11", "Eid al Fitr Holiday", estimated=True, description=_MOON),
        _h("2027-05-16", "Arafat Day", estimated=True, description=_MOON),
        _h("2027-05-17", "Eid al Adha", estimated=True, description=_MOON),
        _h("2027-05-18", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2027-05-19", "Eid al Adha Holiday", estimated=True, description=_MOON),
        _h("2027-06-07", "Islamic New Year", estimated=True, description=_MOON),
        _h("2027-08-15", "Prophet Muhammad's Birthday", estimated=True, description=_MOON),
        _h("2027-12-01", "Commemoration Day"),
        _h("2027-12-02", "National Day"),
        _h("2027-12-03", "National Day Holiday"),
    ],
}

# Fixed-date federal holidays, used when a curated country is asked for a year
# outside the curated window. Always correct, deliberately incomplete — the
# response marks the calendar `partial` so the UI can say so.
_FIXED_ONLY: Dict[str, List[Tuple[str, str, str]]] = {
    "IN": [
        ("01-01", "New Year's Day", HOLIDAY_TYPE_OPTIONAL),
        ("01-26", "Republic Day", HOLIDAY_TYPE_PUBLIC),
        ("05-01", "Labour Day", HOLIDAY_TYPE_PUBLIC),
        ("08-15", "Independence Day", HOLIDAY_TYPE_PUBLIC),
        ("10-02", "Gandhi Jayanti", HOLIDAY_TYPE_PUBLIC),
        ("12-25", "Christmas", HOLIDAY_TYPE_PUBLIC),
    ],
    "AE": [
        ("01-01", "New Year's Day", HOLIDAY_TYPE_PUBLIC),
        ("12-01", "Commemoration Day", HOLIDAY_TYPE_PUBLIC),
        ("12-02", "National Day", HOLIDAY_TYPE_PUBLIC),
        ("12-03", "National Day Holiday", HOLIDAY_TYPE_PUBLIC),
    ],
}

CURATED_CALENDARS: Dict[str, Dict[int, List[Dict[str, Any]]]] = {
    "IN": _INDIA,
    "AE": _UAE,
}

# Display names for the curated countries. Provider-backed countries take their
# name from the provider's own /AvailableCountries response.
CURATED_COUNTRY_NAMES: Dict[str, str] = {
    "IN": "India",
    "AE": "United Arab Emirates",
}


# ── Public helpers ────────────────────────────────────────────────────────────
def is_valid_country_code(code: Any) -> bool:
    """ISO 3166-1 alpha-2 shape check. Format only — not a coverage check."""
    text = str(code or "").strip().upper()
    return len(text) == 2 and text.isalpha() and text.isascii()


def normalize_country_code(code: Any) -> Optional[str]:
    """Upper-cased ISO-2, or None when the input is not ISO-2 shaped."""
    text = str(code or "").strip().upper()
    return text if is_valid_country_code(text) else None


def is_valid_year(year: Any) -> bool:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return False
    return MIN_YEAR <= y <= MAX_YEAR


def has_curated_calendar(code: str) -> bool:
    return normalize_country_code(code) in CURATED_CALENDARS


def curated_countries() -> List[Dict[str, str]]:
    """The countries we ship calendars for, alphabetical by name."""
    return sorted(
        (
            {"country_code": cc, "country_name": CURATED_COUNTRY_NAMES.get(cc, cc)}
            for cc in CURATED_CALENDARS
        ),
        key=lambda c: c["country_name"],
    )


def _fixed_only(code: str, year: int) -> List[Dict[str, Any]]:
    entries = _FIXED_ONLY.get(code, [])
    return [
        _h(f"{year}-{md}", name, htype)
        for md, name, htype in entries
    ]


def curated_calendar(code: Any, year: Any) -> Optional[Dict[str, Any]]:
    """The curated calendar for a country + year, already normalized.

    Returns ``None`` when we ship nothing for that country — the caller then
    decides between the external provider and an ``unsupported`` answer. A
    curated country asked for a year outside the curated window gets its
    fixed-date subset with ``partial=True`` rather than an empty list, because
    "Republic Day is on 26 January" is true in every year.
    """
    cc = normalize_country_code(code)
    if cc is None or cc not in CURATED_CALENDARS:
        return None
    if not is_valid_year(year):
        return None
    y = int(year)

    raw = CURATED_CALENDARS[cc].get(y)
    partial = raw is None
    if partial:
        raw = _fixed_only(cc, y)

    return {
        "country_code": cc,
        "country_name": CURATED_COUNTRY_NAMES.get(cc, cc),
        "year": y,
        "source": SUPPORT_CURATED,
        "partial": partial,
        "holidays": [normalize_entry(e, source=SUPPORT_CURATED) for e in raw],
    }


def weekday_name(value: Any) -> str:
    """English weekday for a date or a YYYY-MM-DD string; "" when unparseable.

    Deliberately English and locale-free: this is the machine-readable `day`
    field. Presentation locale is the frontend's job (it formats from `date`),
    which is what removed the hardcoded ``en-IN`` from the admin UI.
    """
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        return value.strftime("%A")
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d").date().strftime("%A")
    except (TypeError, ValueError):
        return ""


def normalize_entry(entry: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    """Coerce a catalog or provider entry into the single normalized shape.

    Every holiday that leaves the backend — curated, provider-sourced or read
    from the ``holidays`` table — has these keys, so the table, the calendar,
    the CSV preview and the employee views all consume one contract.

    ``regional`` / ``regions`` are SEPARATE from ``holiday_type`` on purpose.
    They used to be the same field: a provider holiday with a non-empty
    ``counties`` array was downgraded to ``Optional``, which destroyed
    information in both directions —

      * UK New Year's Day lists all four constituent countries, so it is
        nationwide, yet it became ``Optional`` and was skipped on adoption;
      * India's New Year's Day is genuinely ``Optional`` (restricted) and
        nationwide, yet the Settings page rendered it as "Regional".

    "Is this a mandatory day off?" and "who observes it?" are independent
    questions and now have independent fields.
    """
    date_str = str(entry.get("date") or "").strip()
    regions = entry.get("regions") or []
    if not isinstance(regions, (list, tuple)):
        regions = []
    regions = [str(r).strip() for r in regions if str(r).strip()]
    return {
        "id": entry.get("id"),
        "name": str(entry.get("name") or "").strip(),
        "date": date_str,
        "day": weekday_name(date_str),
        "holiday_type": normalize_holiday_type(entry.get("holiday_type")),
        "description": str(entry.get("description") or "").strip(),
        "estimated": bool(entry.get("estimated")),
        # True only when the holiday is observed in SOME subdivisions, not all.
        "regional": bool(entry.get("regional")),
        # The subdivisions that observe it, when the source tells us.
        "regions": regions,
        "source": source,
    }
