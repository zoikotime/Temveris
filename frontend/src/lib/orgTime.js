/**
 * Temveris — organization-local time formatting.
 * ==============================================
 * Every timestamp the app stores is UTC. Every timestamp the app *shows* must
 * be the organization's wall clock, because that is the clock its employees
 * work to.
 *
 * Until this module existed, roughly 24 call sites passed
 * `timeZone: "Asia/Kolkata"` to `toLocaleTimeString` and ~85 passed the `en-IN`
 * locale. That is correct for exactly one tenant. For any other it renders the
 * wrong hour and, worse, buckets a session onto the wrong calendar day near
 * midnight — which is how a dashboard can disagree with the report beside it.
 *
 * The zone is set ONCE, from `OrgSettings.default_timezone` delivered by
 * `GET /org/settings` (and re-confirmed by the `timezone` field on
 * `/employees/team` rows). It is module state rather than context because the
 * formatters are called from deep inside render trees and pure helpers that
 * have no hooks available; threading a prop through every one of them would be
 * a far larger and more error-prone change for the same result.
 *
 * Business decisions are NOT made here. "Is this person late", "is today a
 * working day", "have they passed the absence cutoff" are all answered by the
 * backend and arrive as `arrival_status` / `is_working_day`. This module only
 * renders instants.
 */

// The browser's own zone is deliberately NOT the default. An admin travelling
// abroad must still see their workspace's clock, not the airport's.
const FALLBACK_TZ = "Asia/Kolkata";
const FALLBACK_LOCALE = "en-GB";

let ORG_TZ = FALLBACK_TZ;
let ORG_LOCALE = FALLBACK_LOCALE;

/**
 * True when `tz` is a zone this browser can actually format in.
 *
 * Exported so the Organization Timezone control validates against the same
 * rule the formatters use. It is a first line of defence only — the backend's
 * `zoneinfo.ZoneInfo` check in `PUT /org/settings` remains the authority, and
 * it is what decides whether a value is ever stored.
 */
export const isUsableZone = (tz) => {
  if (!tz || typeof tz !== "string" || !tz.trim()) return false;
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: tz.trim() }).format(new Date());
    return true;
  } catch {
    return false;
  }
};

// Curated fallback for engines without `Intl.supportedValuesOf` (Safari < 15.4).
// Deliberately short: it only has to keep the picker usable, because the full
// list is available on every browser the product targets.
const FALLBACK_ZONE_LIST = [
  "UTC",
  "Asia/Kolkata", "Asia/Dubai", "Asia/Singapore", "Asia/Tokyo", "Asia/Shanghai",
  "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Madrid",
  "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles",
  "America/Sao_Paulo", "Australia/Sydney", "Africa/Johannesburg", "Pacific/Auckland",
];

// Names the backend accepts and the product uses, which the browser's list can
// omit. `Intl.supportedValuesOf` reports CLDR-canonical ids, so it returns
// "Asia/Calcutta" and NOT "Asia/Kolkata" — the very name this product's docs,
// settings and support conversations use. `zoneinfo` accepts both. Without
// this the picker could not offer an Indian workspace its own timezone.
// UTC is likewise absent from the list on most engines.
const EXTRA_SELECTABLE = ["UTC", "Asia/Kolkata"];

/**
 * Every IANA zone the Organization Timezone picker offers.
 *
 * `Intl.supportedValuesOf` is the platform's own tz database — no dependency
 * and no hand-maintained list that drifts when a country changes its rules —
 * plus the aliases above. Every entry is filtered through `isUsableZone`, so
 * the picker can never offer a value this browser cannot format.
 */
export const listIanaZones = () => {
  let zones = FALLBACK_ZONE_LIST;
  try {
    const supported = Intl.supportedValuesOf("timeZone");
    if (Array.isArray(supported) && supported.length) zones = supported;
  } catch { /* keep the fallback list */ }
  return [...new Set([...EXTRA_SELECTABLE, ...zones])]
    .filter(isUsableZone)
    .sort();
};

const SELECTABLE = new Set(listIanaZones());

/**
 * True when `tz` is one of the zones the picker offers.
 *
 * STRICTER than `isUsableZone`, and deliberately so. `Intl.DateTimeFormat`
 * happily accepts "IST", "EST" and "+05:30"; Python's `zoneinfo` — the backend
 * authority — rejects the first and the third. Validating the settings field
 * with the loose check would let an admin save a value the API then refuses
 * with a 400, which is a worse experience than not offering it at all.
 *
 * Membership in the offered list is the honest client-side question: "is this
 * one of the identifiers we present?" The backend still has the final say.
 */
export const isSelectableZone = (tz) =>
  typeof tz === "string" && SELECTABLE.has(tz.trim());

// Zones whose CLDR-canonical id differs from the name people actually use, so
// a search for either spelling finds the entry. `Intl.supportedValuesOf`
// reports "Asia/Calcutta"; every Indian admin will type "Kolkata".
const ZONE_ALIASES = {
  "Asia/Calcutta": "asia/kolkata",
  "Asia/Kolkata": "asia/calcutta",
  "Asia/Katmandu": "asia/kathmandu",
  "Asia/Rangoon": "asia/yangon",
  "America/Godthab": "america/nuuk",
  "Europe/Kiev": "europe/kyiv",
};

/**
 * Lowercased haystack for filtering a zone: its identifier, the city with
 * underscores as spaces, and any alternate spelling. Keeps "Los Angeles",
 * "los_angeles" and "Kolkata" all finding the right row.
 */
export const zoneSearchText = (zone) => {
  const base = zone.toLowerCase();
  return `${base} ${base.replace(/_/g, " ")} ${ZONE_ALIASES[zone] || ""}`.trim();
};

// The canonical id we would rather an admin end up storing when two spellings
// mean the same zone. Browsers report "Asia/Calcutta"; the product, its docs
// and its support conversations all say "Asia/Kolkata".
const PREFERRED_ALIAS = { "Asia/Calcutta": "Asia/Kolkata" };

// Labels cost ~0.7ms each to build (two Intl formatters), so all 420 would be
// ~450ms of jank the moment the picker opens. Memoized and computed only for
// the rows actually rendered.
const _labelCache = new Map();

/**
 * Human-readable description of a zone, for display only.
 *
 * `{ id, name, offset, sameAs, label }` — e.g.
 *   id "Asia/Kolkata", name "India Standard Time", offset "GMT+5:30",
 *   label "Asia/Kolkata — India Standard Time (GMT+5:30)"
 *
 * The IDENTIFIER remains what is stored and sent to the API; this is what an
 * administrator reads. Degrades to the bare identifier on any engine that
 * cannot produce a zone name, so the picker is never blank.
 */
export const zoneLabel = (zone) => {
  if (_labelCache.has(zone)) return _labelCache.get(zone);
  let name = "";
  let offset = "";
  try {
    const part = (style) =>
      new Intl.DateTimeFormat("en-GB", { timeZone: zone, timeZoneName: style })
        .formatToParts(new Date())
        .find((p) => p.type === "timeZoneName")?.value || "";
    name = part("long");
    offset = part("shortOffset");
  } catch { /* fall back to the identifier alone */ }

  const sameAs = PREFERRED_ALIAS[zone] || "";
  const detail = [name, offset && `(${offset})`].filter(Boolean).join(" ");
  const info = _labelCache.get(zone) || {
    id: zone,
    name,
    offset,
    sameAs,
    label: detail ? `${zone} — ${detail}` : zone,
  };
  _labelCache.set(zone, info);
  return info;
};

/**
 * The zone list grouped by region, for a native `<select>`'s `<optgroup>`s.
 *
 * ~420 flat options is a wall of text to scroll; the region prefix is already
 * part of every identifier, so grouping costs one split and makes the list
 * navigable ("Asia" → 90 entries instead of 420). Zones with no prefix — UTC —
 * are grouped under "Other" so nothing is dropped.
 *
 * Returns `[{ region, zones: [...] }]`, regions alphabetical, zones sorted
 * within each.
 */
export const groupedIanaZones = () => {
  const groups = new Map();
  for (const zone of listIanaZones()) {
    const region = zone.includes("/") ? zone.split("/")[0] : "Other";
    if (!groups.has(region)) groups.set(region, []);
    groups.get(region).push(zone);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([region, zones]) => ({ region, zones: zones.sort() }));
};

/**
 * Point the formatters at this organization's zone.
 *
 * Ignores anything unusable — production holds free text like
 * "California, Sacramento" for at least one workspace, and an invalid zone
 * makes `toLocaleString` throw, which would blank out every time on the page.
 */
export const setOrgTimeZone = (tz) => {
  ORG_TZ = isUsableZone(tz) ? tz.trim() : FALLBACK_TZ;
  return ORG_TZ;
};

export const getOrgTimeZone = () => ORG_TZ;

export const setOrgLocale = (locale) => {
  ORG_LOCALE = (locale && String(locale).trim()) || FALLBACK_LOCALE;
  return ORG_LOCALE;
};

export const getOrgLocale = () => ORG_LOCALE;

/** Parse a stored timestamp, tolerating the legacy "+00:00Z" form. */
export const parseUTC = (s) => {
  if (s instanceof Date) return s;
  let v = String(s);
  if (v.endsWith("+00:00Z")) v = v.slice(0, -1);
  return new Date(/[Z+]/.test(v.slice(-6)) ? v : v + "Z");
};

/** "09:05 AM" on the organization's clock. */
export const fmtTime = (value, opts = {}) => {
  if (!value) return "—";
  try {
    return parseUTC(value).toLocaleTimeString(ORG_LOCALE, {
      hour: "2-digit", minute: "2-digit", hour12: true,
      timeZone: ORG_TZ, ...opts,
    });
  } catch { return "—"; }
};

/** "YYYY-MM-DD" on the organization's clock — the key every "is this today?"
 *  comparison must use, so the day rolls over at the workspace's midnight. */
export const dateKey = (value) => {
  if (!value) return "";
  try {
    return parseUTC(value).toLocaleDateString("en-CA", { timeZone: ORG_TZ });
  } catch { return ""; }
};

/** Today's date key on the organization's clock. */
export const orgToday = () => dateKey(new Date());

export const fmtDateTime = (value, opts = {}) => {
  if (!value) return "—";
  try {
    return parseUTC(value).toLocaleString(ORG_LOCALE, {
      day: "numeric", month: "short", hour: "2-digit", minute: "2-digit",
      hour12: true, timeZone: ORG_TZ, ...opts,
    });
  } catch { return "—"; }
};

/** Short zone label for a UI hint, e.g. "Kolkata", "New York". */
export const orgZoneLabel = () => ORG_TZ.split("/").pop().replace(/_/g, " ");

/**
 * Milliseconds the organization's zone is ahead of UTC at `date`.
 * DST-correct because it asks Intl for the actual local wall clock.
 */
const zoneOffsetMs = (date) => {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-US", {
      timeZone: ORG_TZ, hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    })
      .formatToParts(date)
      .filter((p) => p.type !== "literal")
      .map((p) => [p.type, p.value]),
  );
  const asUTC = Date.UTC(
    +parts.year, +parts.month - 1, +parts.day,
    +parts.hour % 24, +parts.minute, +parts.second,
  );
  return asUTC - date.getTime();
};

/**
 * [start, end) UTC instants bracketing one calendar day on the organization's
 * clock. Replaces the `- 5.5 * 3600 * 1000` arithmetic that appeared wherever
 * a date picker's YYYY-MM-DD had to become a query window: that offset is
 * correct for exactly one timezone and ignores DST everywhere else.
 */
export const orgDayBoundsUTC = (dayKey) => {
  const naive = new Date(`${dayKey}T00:00:00Z`);
  // Two passes so a day that starts inside a DST transition still lands right.
  let ms = naive.getTime() - zoneOffsetMs(naive);
  ms = naive.getTime() - zoneOffsetMs(new Date(ms));
  const start = new Date(ms);
  const endNaive = new Date(naive.getTime() + 24 * 3600 * 1000);
  let endMs = endNaive.getTime() - zoneOffsetMs(endNaive);
  endMs = endNaive.getTime() - zoneOffsetMs(new Date(endMs));
  return [start, new Date(endMs)];
};

/** Hour-of-day (fractional) of a UTC instant on the organization's clock. */
export const orgHourOf = (value) => {
  const d = parseUTC(value);
  if (Number.isNaN(d.getTime())) return 0;
  const local = new Date(d.getTime() + zoneOffsetMs(d));
  return local.getUTCHours() + local.getUTCMinutes() / 60;
};
