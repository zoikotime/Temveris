/**
 * api/holidays.js — the single holiday client.
 *
 * Pins the two things the audit found broken at this layer:
 *
 *  1. Every failure mode collapsed into one message ("Could not load holidays
 *     for this country"), so an admin could not tell an unsupported country
 *     from a provider outage from a dead network.
 *  2. Dates were formatted with a hardcoded "en-IN" everywhere in the holiday
 *     UI, regardless of the language the workspace had selected.
 *
 * `authFetch` is mocked so these run without a server.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

vi.mock("../api/authFetch", () => ({
  authFetch: vi.fn(),
  authFetchJson: vi.fn(),
  default: vi.fn(),
}));
vi.mock("../config", () => ({ API_BASE: "" }));

import { authFetch } from "../api/authFetch";
import * as holidayApi from "../api/holidays";

const ok = (body) => ({
  ok: true,
  status: 200,
  json: async () => body,
});

const fail = (status, detail) => ({
  ok: false,
  status,
  json: async () => ({ detail }),
});

const failNonJson = (status) => ({
  ok: false,
  status,
  json: async () => { throw new SyntaxError("Unexpected token <"); },
});

beforeEach(() => { authFetch.mockReset(); });
afterEach(() => { vi.clearAllMocks(); });

// ── Error taxonomy ───────────────────────────────────────────────────────────
describe("error kinds stay distinct", () => {
  it("an unsupported country is its own kind, not a generic failure", async () => {
    authFetch.mockResolvedValue(fail(404, {
      kind: "unsupported_country", message: "Nager.Date has no calendar for this country.",
    }));
    await expect(holidayApi.fetchCountryCalendar("ZZ", 2026)).rejects.toMatchObject({
      kind: holidayApi.HOLIDAY_ERROR.UNSUPPORTED_COUNTRY,
      status: 404,
    });
  });

  it("a provider outage is a DIFFERENT kind from an unsupported country", async () => {
    authFetch.mockResolvedValue(fail(504, {
      kind: "provider_unavailable", message: "Nager.Date did not respond in time.",
    }));
    const err = await holidayApi.fetchCountryCalendar("FR", 2026).catch((e) => e);
    expect(err.kind).toBe(holidayApi.HOLIDAY_ERROR.PROVIDER_UNAVAILABLE);
    expect(err.kind).not.toBe(holidayApi.HOLIDAY_ERROR.UNSUPPORTED_COUNTRY);
  });

  it("an invalid year is reported as such", async () => {
    authFetch.mockResolvedValue(fail(400, { kind: "invalid_year", message: "bad year" }));
    await expect(holidayApi.fetchCountryCalendar("US", 99999)).rejects.toMatchObject({
      kind: holidayApi.HOLIDAY_ERROR.INVALID_YEAR,
    });
  });

  it("a 403 is a permission problem, not a holiday problem", async () => {
    authFetch.mockResolvedValue(fail(403, "Admin or HR access required"));
    await expect(holidayApi.createHoliday({ name: "X", date: "2026-01-01" }))
      .rejects.toMatchObject({ kind: holidayApi.HOLIDAY_ERROR.FORBIDDEN });
  });

  it("a network failure is distinguishable from a server response", async () => {
    authFetch.mockRejectedValue(new TypeError("Failed to fetch"));
    await expect(holidayApi.fetchCountries()).rejects.toMatchObject({
      kind: holidayApi.HOLIDAY_ERROR.NETWORK,
    });
  });

  it("a non-JSON error body does not become an unhandled SyntaxError", async () => {
    // This is how a plain-text 500 used to be swallowed: `r.json()` threw
    // inside a bare catch and the UI showed nothing at all.
    authFetch.mockResolvedValue(failNonJson(500));
    const err = await holidayApi.fetchCountries().catch((e) => e);
    expect(err).toBeInstanceOf(holidayApi.HolidayApiError);
    expect(err.kind).toBe(holidayApi.HOLIDAY_ERROR.UNKNOWN);
  });

  it("every kind has its own message", () => {
    const messages = Object.values(holidayApi.HOLIDAY_ERROR)
      .map((k) => holidayApi.HOLIDAY_ERROR_MESSAGE[k]);
    expect(new Set(messages).size).toBe(messages.length);
    expect(messages.every(Boolean)).toBe(true);
  });

  it("the unsupported-country message tells the admin what they CAN do", () => {
    expect(holidayApi.HOLIDAY_ERROR_MESSAGE[holidayApi.HOLIDAY_ERROR.UNSUPPORTED_COUNTRY])
      .toMatch(/manually/i);
  });

  it("the provider-unavailable message invites a retry", () => {
    expect(holidayApi.HOLIDAY_ERROR_MESSAGE[holidayApi.HOLIDAY_ERROR.PROVIDER_UNAVAILABLE])
      .toMatch(/try again/i);
  });
});

// ── Requests ─────────────────────────────────────────────────────────────────
describe("requests go to Temveris, never to an external provider", () => {
  it("the country list hits our API", async () => {
    authFetch.mockResolvedValue(ok({ countries: [] }));
    await holidayApi.fetchCountries();
    expect(authFetch.mock.calls[0][0]).toBe("/holidays/countries");
  });

  it("no holiday call reaches date.nager.at", async () => {
    authFetch.mockResolvedValue(ok({
      country_code: "IN", year: 2026, source: "curated", holidays: [],
    }));
    await holidayApi.fetchCountryCalendar("IN", 2026);
    authFetch.mockResolvedValue(ok([]));
    await holidayApi.fetchOrgHolidays({ year: 2026, countryCode: "IN" });
    for (const [url] of authFetch.mock.calls) {
      expect(url).not.toMatch(/nager/i);
      expect(url).not.toMatch(/^https?:\/\//);
    }
  });

  it("the catalog request carries country and year", async () => {
    authFetch.mockResolvedValue(ok({
      country_code: "IN", year: 2026, source: "curated", holidays: [],
    }));
    await holidayApi.fetchCountryCalendar("IN", 2026);
    expect(authFetch.mock.calls[0][0]).toBe("/holidays/catalog?country_code=IN&year=2026");
  });

  it("org holidays with no filters ask for everything", async () => {
    authFetch.mockResolvedValue(ok([]));
    await holidayApi.fetchOrgHolidays();
    expect(authFetch.mock.calls[0][0]).toBe("/holidays");
  });

  it("org holidays send both filters when both are set", async () => {
    authFetch.mockResolvedValue(ok([]));
    await holidayApi.fetchOrgHolidays({ year: 2027, countryCode: "US" });
    expect(authFetch.mock.calls[0][0]).toBe("/holidays?year=2027&country_code=US");
  });

  it("create sends country_code", async () => {
    authFetch.mockResolvedValue(ok({ id: 1 }));
    await holidayApi.createHoliday({ name: "Diwali", date: "2026-11-08", country_code: "IN" });
    const [url, init] = authFetch.mock.calls[0];
    expect(url).toBe("/holidays");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body).country_code).toBe("IN");
  });

  it("update uses PUT on the row id", async () => {
    authFetch.mockResolvedValue(ok({}));
    await holidayApi.updateHoliday(42, { name: "X", date: "2026-01-01" });
    const [url, init] = authFetch.mock.calls[0];
    expect(url).toBe("/holidays/42");
    expect(init.method).toBe("PUT");
  });

  it("delete uses DELETE and surfaces a 404", async () => {
    authFetch.mockResolvedValue(fail(404, "Holiday not found"));
    const err = await holidayApi.deleteHoliday(999).catch((e) => e);
    expect(authFetch.mock.calls[0][1].method).toBe("DELETE");
    expect(err.status).toBe(404);
  });

  it("CSV upload carries the date format and country", async () => {
    authFetch.mockResolvedValue(ok({ imported: 3 }));
    const file = new File(["name,date\nX,2026-01-01"], "h.csv", { type: "text/csv" });
    await holidayApi.importHolidayCsv(file, { dateFormat: "dmy", countryCode: "IN" });
    const [url, init] = authFetch.mock.calls[0];
    expect(url).toBe("/holidays/import-csv");
    expect(init.body.get("date_format")).toBe("dmy");
    expect(init.body.get("country_code")).toBe("IN");
  });

  it("CSV upload omits the country when none is selected", async () => {
    authFetch.mockResolvedValue(ok({ imported: 0 }));
    const file = new File(["name,date"], "h.csv", { type: "text/csv" });
    await holidayApi.importHolidayCsv(file, {});
    expect(authFetch.mock.calls[0][1].body.get("country_code")).toBeNull();
  });
});

// ── Formatting ───────────────────────────────────────────────────────────────
describe("dates are formatted in the viewer's locale, not en-IN", () => {
  it("the same date renders differently in different locales", () => {
    const us = holidayApi.formatHolidayDate("2026-11-08", "en-US");
    const de = holidayApi.formatHolidayDate("2026-11-08", "de-DE");
    expect(us).toBeTruthy();
    expect(de).toBeTruthy();
    expect(us).not.toBe(de);
  });

  it("an undefined locale falls back to the browser default without throwing", () => {
    expect(holidayApi.formatHolidayDate("2026-11-08", undefined)).toBeTruthy();
  });

  it("an unparseable date is returned as-is rather than 'Invalid Date'", () => {
    expect(holidayApi.formatHolidayDate("garbage", "en-US")).toBe("garbage");
    expect(holidayApi.formatHolidayDate("", "en-US")).toBe("");
  });

  it("the weekday is localized and falls back to the API value", () => {
    expect(holidayApi.formatHolidayWeekday("2026-01-26", "en-US")).toBe("Monday");
    expect(holidayApi.formatHolidayWeekday("nonsense", "en-US", "Monday")).toBe("Monday");
  });

  it("the month label is localized", () => {
    expect(holidayApi.formatMonthLabel(2026, 0, "en-US")).toMatch(/January/);
  });
});

// ── Year options ─────────────────────────────────────────────────────────────
describe("year selector options", () => {
  it("covers a window around the current year", () => {
    const current = new Date().getFullYear();
    const years = holidayApi.buildYearOptions([]);
    expect(years).toContain(current);
    expect(years).toContain(current + 1);
    expect(years).toContain(current - 1);
  });

  it("widens to include years the organization already has data for", () => {
    const years = holidayApi.buildYearOptions([2019, 2035]);
    expect(years).toContain(2019);
    expect(years).toContain(2035);
  });

  it("is sorted and free of duplicates", () => {
    const current = new Date().getFullYear();
    const years = holidayApi.buildYearOptions([current, current, 2019]);
    expect(years).toEqual([...years].sort((a, b) => a - b));
    expect(new Set(years).size).toBe(years.length);
  });

  it("ignores junk entries", () => {
    const years = holidayApi.buildYearOptions([null, "nope", undefined]);
    expect(years.every(Number.isFinite)).toBe(true);
  });
});

// ── Shared vocabulary ────────────────────────────────────────────────────────
describe("shared constants", () => {
  it("holiday types match the backend catalog", () => {
    expect(holidayApi.HOLIDAY_TYPES).toEqual(["Public", "Company", "Optional"]);
  });
});

// ── A 200 is not proof of success ────────────────────────────────────────────
describe("malformed 200 responses are errors, not empty calendars", () => {
  // This is not hypothetical. main.py serves an SPA catch-all, so calling an
  // API route the running server does not have returns
  // `200 {"detail":"Not Found"}`. With a backend one version behind, the
  // country dropdown silently rendered empty and said nothing was wrong.
  const spaCatchAll = { ok: true, status: 200, json: async () => ({ detail: "Not Found" }) };

  it("the SPA catch-all is rejected, not read as zero countries", async () => {
    authFetch.mockResolvedValue(spaCatchAll);
    const err = await holidayApi.fetchCountries().catch((e) => e);
    expect(err).toBeInstanceOf(holidayApi.HolidayApiError);
    expect(err.message).toMatch(/route not found/i);
  });

  it("the error names the route so the cause is obvious", async () => {
    authFetch.mockResolvedValue(spaCatchAll);
    const err = await holidayApi.fetchCountryCalendar("IN", 2026).catch((e) => e);
    expect(err.message).toContain("/holidays/catalog");
    expect(err.message).toMatch(/different version/i);
  });

  it("a 200 missing the countries array is rejected", async () => {
    authFetch.mockResolvedValue(ok({ provider: "Nager.Date" }));
    await expect(holidayApi.fetchCountries()).rejects.toThrow(/missing countries/i);
  });

  it("countries present but not a list is rejected", async () => {
    authFetch.mockResolvedValue(ok({ countries: "nope" }));
    await expect(holidayApi.fetchCountries()).rejects.toThrow(/expected a list/i);
  });

  it("a catalog response missing holidays is rejected", async () => {
    authFetch.mockResolvedValue(ok({ country_code: "IN", year: 2026, source: "curated" }));
    await expect(holidayApi.fetchCountryCalendar("IN", 2026))
      .rejects.toThrow(/missing holidays/i);
  });

  it("an adoption plan missing entries is rejected", async () => {
    authFetch.mockResolvedValue(ok({ country_code: "IN", year: 2026, counts: {} }));
    await expect(holidayApi.fetchAdoptionPlan("IN", 2026))
      .rejects.toThrow(/missing entries/i);
  });

  it("an org-holiday list that is not an array is rejected", async () => {
    authFetch.mockResolvedValue(ok({ detail: "Not Found" }));
    await expect(holidayApi.fetchOrgHolidays()).rejects.toThrow(/expected a list/i);
  });

  it("an import result missing its counts is rejected", async () => {
    authFetch.mockResolvedValue(ok({ detail: "Not Found" }));
    await expect(holidayApi.importCountryCalendar({ countryCode: "IN", year: 2026 }))
      .rejects.toThrow();
  });

  it("a genuinely EMPTY but well-formed calendar still succeeds", async () => {
    // The whole point: empty must stay distinguishable from broken.
    authFetch.mockResolvedValue(ok({
      country_code: "XX", year: 2026, source: "provider", holidays: [],
    }));
    const payload = await holidayApi.fetchCountryCalendar("XX", 2026);
    expect(payload.holidays).toEqual([]);
  });

  it("an empty country list that is well-formed still succeeds", async () => {
    authFetch.mockResolvedValue(ok({ countries: [], provider_available: false }));
    const payload = await holidayApi.fetchCountries();
    expect(payload.countries).toEqual([]);
  });
});

// ── Adoption calls ───────────────────────────────────────────────────────────
describe("adoption requests", () => {
  const planOk = ok({
    country_code: "IN", year: 2026, counts: { will_add: 9 }, entries: [],
  });

  it("the plan request carries the toggles", async () => {
    authFetch.mockResolvedValue(planOk);
    await holidayApi.fetchAdoptionPlan("IN", 2026,
      { includeOptional: true, includeRegional: false });
    const url = authFetch.mock.calls[0][0];
    expect(url).toContain("/holidays/catalog/plan?");
    expect(url).toContain("country_code=IN");
    expect(url).toContain("year=2026");
    expect(url).toContain("include_optional=true");
    expect(url).toContain("include_regional=false");
  });

  it("Add-all sends no keys", async () => {
    authFetch.mockResolvedValue(ok({ added: 9, skipped: 13 }));
    await holidayApi.importCountryCalendar({ countryCode: "IN", year: 2026 });
    const body = JSON.parse(authFetch.mock.calls[0][1].body);
    expect(body.keys).toBeUndefined();
    expect(body.resolutions).toBeUndefined();
    expect(body.include_optional).toBe(false);
    expect(body.include_regional).toBe(false);
  });

  it("per-row Add sends exactly one key", async () => {
    authFetch.mockResolvedValue(ok({ added: 1, skipped: 0 }));
    await holidayApi.importCountryCalendar({
      countryCode: "IN", year: 2026, keys: ["2026-11-08|diwali"],
    });
    expect(JSON.parse(authFetch.mock.calls[0][1].body).keys)
      .toEqual(["2026-11-08|diwali"]);
  });

  it("a conflict decision travels as a resolution", async () => {
    authFetch.mockResolvedValue(ok({ added: 0, replaced: 1, skipped: 0 }));
    await holidayApi.importCountryCalendar({
      countryCode: "IN", year: 2026,
      keys: ["2026-05-27|bakrid eid ul adha"],
      resolutions: { "2026-05-27|bakrid eid ul adha": "replace" },
    });
    const body = JSON.parse(authFetch.mock.calls[0][1].body);
    expect(body.resolutions["2026-05-27|bakrid eid ul adha"]).toBe("replace");
  });

  it("empty keys and resolutions are omitted rather than sent empty", async () => {
    authFetch.mockResolvedValue(ok({ added: 0, skipped: 0 }));
    await holidayApi.importCountryCalendar({
      countryCode: "IN", year: 2026, keys: [], resolutions: {},
    });
    const body = JSON.parse(authFetch.mock.calls[0][1].body);
    expect("keys" in body).toBe(false);
    expect("resolutions" in body).toBe(false);
  });
});
