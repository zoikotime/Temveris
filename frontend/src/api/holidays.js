/**
 * holidays — the single client for every holiday call.
 *
 * Why this exists
 * ---------------
 * Holiday logic used to be spread across two pages that had drifted apart:
 *
 *   AdminDashboard.jsx  fetch("https://date.nager.at/api/v3/PublicHolidays/…")
 *                       + a localStorage cache that never expired
 *   SettingsPage.jsx    fetch("https://date.nager.at/api/v3/AvailableCountries")
 *                       + a second, cache-less copy of the same call
 *                       + a 21-entry Indian calendar inlined in the module
 *
 * Nager.Date does not cover India, so AdminDashboard — which had no fallback —
 * answered "Could not load holidays for this country" for the product's primary
 * market. SettingsPage had a fallback; AdminDashboard never got it.
 *
 * The browser no longer talks to any external holiday provider. Everything goes
 * through the Temveris API, which picks between a curated calendar and the
 * upstream provider, caches server-side and normalizes the response. Both pages
 * import from here, so a fix can only ever be applied in one place.
 *
 * Error contract
 * --------------
 * Every function rejects with a `HolidayApiError` carrying a machine-readable
 * `kind`. Callers switch on `kind` — never on the message — so the UI can tell
 * "this country has no calendar" from "the provider is down", which the single
 * red string it used to show could not.
 */
import { API_BASE } from "../config";
import { authFetch } from "./authFetch";

/** Failure kinds. Mirrors the `kind` the backend puts in `detail`. */
export const HOLIDAY_ERROR = {
  INVALID_COUNTRY: "invalid_country",
  INVALID_YEAR: "invalid_year",
  UNSUPPORTED_COUNTRY: "unsupported_country",
  PROVIDER_UNAVAILABLE: "provider_unavailable",
  FORBIDDEN: "forbidden",
  NETWORK: "network_error",
  UNKNOWN: "unknown_error",
};

/** Human wording for each kind. One message per real cause, not one for all. */
export const HOLIDAY_ERROR_MESSAGE = {
  [HOLIDAY_ERROR.INVALID_COUNTRY]: "That country code is not valid.",
  [HOLIDAY_ERROR.INVALID_YEAR]: "That year is outside the supported range.",
  [HOLIDAY_ERROR.UNSUPPORTED_COUNTRY]:
    "Public holidays are not available from the external provider for this country. " +
    "You can still add this country's holidays manually.",
  [HOLIDAY_ERROR.PROVIDER_UNAVAILABLE]:
    "The holiday provider is temporarily unavailable. Please try again.",
  [HOLIDAY_ERROR.FORBIDDEN]: "You do not have permission to manage holidays.",
  [HOLIDAY_ERROR.NETWORK]:
    "Could not reach Temveris. Check your connection and try again.",
  [HOLIDAY_ERROR.UNKNOWN]: "Something went wrong loading holidays.",
};

export class HolidayApiError extends Error {
  constructor(kind, message, status) {
    super(message || HOLIDAY_ERROR_MESSAGE[kind] || HOLIDAY_ERROR_MESSAGE.unknown_error);
    this.name = "HolidayApiError";
    this.kind = kind;
    this.status = status;
  }
}

/** Shared holiday type vocabulary — matches app/holidays/catalog.py. */
export const HOLIDAY_TYPES = ["Public", "Company", "Optional"];

/**
 * Read a non-OK response into a HolidayApiError.
 * A non-JSON body (a proxy 502, an HTML error page) must not become an
 * unhandled `SyntaxError` — that is how the old code turned a real server
 * error into a silent no-op.
 */
async function toError(res) {
  let detail = null;
  try {
    const body = await res.json();
    detail = body?.detail ?? null;
  } catch {
    detail = null;
  }
  if (res.status === 403) {
    return new HolidayApiError(HOLIDAY_ERROR.FORBIDDEN, null, 403);
  }
  if (detail && typeof detail === "object" && detail.kind) {
    return new HolidayApiError(detail.kind, detail.message, res.status);
  }
  const message = typeof detail === "string" ? detail : null;
  if (res.status === 404) {
    return new HolidayApiError(HOLIDAY_ERROR.UNSUPPORTED_COUNTRY, message, 404);
  }
  return new HolidayApiError(HOLIDAY_ERROR.UNKNOWN, message, res.status);
}

/**
 * A 200 is not proof of success.
 *
 * main.py serves a single-page-app catch-all, so a request to an API route the
 * running server does not have comes back as `200 {"detail":"Not Found"}`.
 * That is exactly what happened when the backend process was a version behind:
 * `fetchCountries` read `payload.countries`, got `undefined`, fell back to `[]`,
 * and the admin saw an empty country dropdown with no error at all — the same
 * "cannot tell empty from broken" failure this module exists to prevent, one
 * layer up.
 *
 * Every reader therefore states the shape it expects, and a 200 that does not
 * match it is an error.
 */
function assertShape(payload, { object = false, array = false, keys = [], path }) {
  const bad = (why) =>
    new HolidayApiError(
      HOLIDAY_ERROR.UNKNOWN,
      `The server sent an unexpected response for ${path} (${why}). ` +
      "The API may be a different version than this page.",
    );

  if (array) {
    if (!Array.isArray(payload)) throw bad("expected a list");
    return payload;
  }
  if (object) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw bad("expected an object");
    }
    // The SPA catch-all's giveaway: an object whose only content is `detail`.
    if (typeof payload.detail === "string" && keys.every((k) => !(k in payload))) {
      throw bad(`route not found — server said "${payload.detail}"`);
    }
    const missing = keys.filter((k) => !(k in payload));
    if (missing.length) throw bad(`missing ${missing.join(", ")}`);
  }
  return payload;
}

async function request(path, init) {
  let res;
  try {
    res = await authFetch(`${API_BASE}${path}`, init);
  } catch {
    throw new HolidayApiError(HOLIDAY_ERROR.NETWORK);
  }
  if (!res.ok) throw await toError(res);
  try {
    return await res.json();
  } catch {
    throw new HolidayApiError(HOLIDAY_ERROR.UNKNOWN, "The server returned an unreadable response.");
  }
}

const json = (body) => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

// ── Country catalog (reference data) ─────────────────────────────────────────

/**
 * Countries the workspace may pick, each tagged `curated` or `provider`.
 * Curated countries are served by Temveris and stay available even when the
 * upstream provider is unreachable — the case that used to produce an empty
 * dropdown.
 */
export async function fetchCountries() {
  const payload = await request("/holidays/countries");
  assertShape(payload, { object: true, keys: ["countries"], path: "/holidays/countries" });
  assertShape(payload.countries, { array: true, path: "/holidays/countries → countries" });
  return payload;
}

/** The public-holiday calendar for a country + year. Read-only preview. */
export async function fetchCountryCalendar(countryCode, year) {
  const params = new URLSearchParams({ country_code: countryCode, year: String(year) });
  const payload = await request(`/holidays/catalog?${params}`);
  assertShape(payload, {
    object: true,
    keys: ["country_code", "year", "source", "holidays"],
    path: "/holidays/catalog",
  });
  assertShape(payload.holidays, { array: true, path: "/holidays/catalog → holidays" });
  return payload;
}

/**
 * What adopting this calendar would do, per entry.
 *
 * Replaces the browser-side "which of these do we already have?" guess. That
 * guess de-duplicated on date+name while the server de-duplicated on
 * (country, date, name); with every legacy holiday carrying a NULL country the
 * two disagreed, the UI offered 13 and the server wrote 20. One endpoint now
 * answers the question for both.
 */
export async function fetchAdoptionPlan(countryCode, year, {
  includeOptional = false, includeRegional = false,
} = {}) {
  const params = new URLSearchParams({
    country_code: countryCode,
    year: String(year),
    include_optional: String(!!includeOptional),
    include_regional: String(!!includeRegional),
  });
  const payload = await request(`/holidays/catalog/plan?${params}`);
  assertShape(payload, {
    object: true,
    keys: ["country_code", "year", "counts", "entries"],
    path: "/holidays/catalog/plan",
  });
  assertShape(payload.entries, { array: true, path: "/holidays/catalog/plan → entries" });
  return payload;
}

// ── The organization's own calendar ──────────────────────────────────────────

/**
 * Stored holidays. Both filters are optional; omitting them returns everything,
 * which is the long-standing contract the employee views depend on.
 * A `countryCode` filter also matches unscoped rows, so holidays that predate
 * country-scoped calendars never disappear from the admin's view.
 */
export async function fetchOrgHolidays({ year, countryCode } = {}) {
  const params = new URLSearchParams();
  if (year) params.set("year", String(year));
  if (countryCode) params.set("country_code", countryCode);
  const qs = params.toString();
  const payload = await request(`/holidays${qs ? `?${qs}` : ""}`);
  return assertShape(payload, { array: true, path: "/holidays" });
}

/** Which country calendars and years this organization actually maintains. */
export function fetchHolidaySummary() {
  return request("/holidays/summary");
}

export function createHoliday(payload) {
  return request("/holidays", json(payload));
}

export function updateHoliday(id, payload) {
  return request(`/holidays/${id}`, {
    ...json(payload),
    method: "PUT",
  });
}

export function deleteHoliday(id) {
  return request(`/holidays/${id}`, { method: "DELETE" });
}

/**
 * Copy catalog holidays into the organization's own calendar.
 *
 * Adds only entries the server classifies `new`. `keys` narrows the run to
 * specific entries — that is how the per-row Add button works, so it shares
 * this one code path rather than having its own. `resolutions` carries the
 * admin's explicit decision for each date conflict:
 * `skip` (default) | `replace` | `add_anyway`.
 */
export async function importCountryCalendar({
  countryCode, year,
  includeOptional = false, includeRegional = false,
  keys = null, resolutions = null,
}) {
  const body = {
    country_code: countryCode,
    year,
    include_optional: includeOptional,
    include_regional: includeRegional,
  };
  if (keys && keys.length) body.keys = keys;
  if (resolutions && Object.keys(resolutions).length) body.resolutions = resolutions;
  const payload = await request("/holidays/import-catalog", json(body));
  return assertShape(payload, {
    object: true,
    keys: ["added", "skipped"],
    path: "/holidays/import-catalog",
  });
}

// ── CSV ──────────────────────────────────────────────────────────────────────

export function fetchCsvOptions() {
  return request("/holidays/import-csv/options");
}

/**
 * Upload a CSV. `dateFormat` is one of auto / iso / dmy / mdy — `auto` rejects
 * ambiguous values like 03/04/2026 rather than silently picking a reading.
 * Resolves to the structured import result (imported / duplicates / invalid).
 */
export function importHolidayCsv(file, { dateFormat = "auto", countryCode = "" } = {}) {
  const form = new FormData();
  form.append("file", file);
  form.append("date_format", dateFormat);
  if (countryCode) form.append("country_code", countryCode);
  return request("/holidays/import-csv", { method: "POST", body: form });
}

// ── Formatting helpers (shared so the two pages cannot drift) ────────────────

/**
 * Format an ISO date for display in the viewer's locale.
 *
 * Replaces the hardcoded `en-IN` that the admin holiday UI used everywhere,
 * which rendered Indian-style dates for every workspace regardless of the
 * language they had selected. API and database dates stay ISO and unambiguous;
 * only presentation is localized.
 */
export function formatHolidayDate(isoDate, locale) {
  if (!isoDate) return "";
  const parsed = new Date(`${isoDate}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return isoDate;
  try {
    return parsed.toLocaleDateString(locale || undefined, {
      day: "2-digit", month: "short", year: "numeric",
    });
  } catch {
    return isoDate;
  }
}

/** Localized weekday. Falls back to the API's English `day` field. */
export function formatHolidayWeekday(isoDate, locale, fallback = "") {
  if (!isoDate) return fallback;
  const parsed = new Date(`${isoDate}T00:00:00`);
  if (Number.isNaN(parsed.getTime())) return fallback;
  try {
    return parsed.toLocaleDateString(locale || undefined, { weekday: "long" });
  } catch {
    return fallback;
  }
}

/** Localized "September 2026" for a calendar header. */
export function formatMonthLabel(year, monthIndex, locale) {
  try {
    return new Date(year, monthIndex, 1).toLocaleDateString(locale || undefined, {
      month: "long", year: "numeric",
    });
  } catch {
    return `${year}-${String(monthIndex + 1).padStart(2, "0")}`;
  }
}

/**
 * Year options for a selector: a window around the current year, widened to
 * include anything the organization already has data for.
 */
export function buildYearOptions(existingYears = [], span = 3) {
  const current = new Date().getFullYear();
  const years = new Set();
  for (let y = current - span; y <= current + span; y += 1) years.add(y);
  (existingYears || []).forEach((y) => {
    const n = Number(y);
    if (Number.isFinite(n)) years.add(n);
  });
  return [...years].sort((a, b) => a - b);
}

/** The message to show for a caught error, whatever its shape. */
export function holidayErrorMessage(error) {
  if (error instanceof HolidayApiError) {
    return error.message || HOLIDAY_ERROR_MESSAGE[error.kind] || HOLIDAY_ERROR_MESSAGE.unknown_error;
  }
  return HOLIDAY_ERROR_MESSAGE[HOLIDAY_ERROR.UNKNOWN];
}
