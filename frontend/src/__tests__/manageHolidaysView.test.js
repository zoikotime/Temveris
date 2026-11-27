/**
 * lib/holidayView — the Manage Holidays page rules that were actually broken.
 *
 * Each block names the audit finding it pins:
 *
 *   * the catalog preview REPLACED the organization's own holidays, so a
 *     holiday an admin had just added vanished the moment they picked a country;
 *   * an error was a TERMINAL page state, so one unsupported country hid the
 *     organization's holidays and froze the Table/Calendar toggle until reload;
 *   * Clear did not reset the error, so the page stayed bricked.
 */
import { describe, it, expect } from "vitest";
import {
  holidayKey,
  mergeHolidayRows,
  holidayPageState,
  selectionResetPatch,
  isRetryableHolidayError,
  upcomingHolidays,
} from "../lib/holidayView";

const org = (name, date, extra = {}) => ({ id: `${name}-${date}`, name, date, ...extra });
// A SERVER-classified plan entry. The browser no longer works out what the
// organization already has — see holidayAdoptView.test.js for that contract,
// and app/holidays/collisions.py for the classification itself.
const cat = (name, date, extra = {}) => ({
  key: `${date}|${name.toLowerCase()}`, name, date,
  status: "new", eligible: true, ...extra,
});

// ── Merging ──────────────────────────────────────────────────────────────────
describe("the catalog preview adds to the organization's holidays, never replaces them", () => {
  it("keeps the organization's own rows when a country is selected", () => {
    const own = [org("Company Retreat", "2026-06-01")];
    const { rows } = mergeHolidayRows(own, [cat("Diwali", "2026-11-08")]);
    expect(rows.map(r => r.name)).toContain("Company Retreat");
    expect(rows.map(r => r.name)).toContain("Diwali");
  });

  it("organization rows come first so they are the ones with actions", () => {
    const { rows } = mergeHolidayRows(
      [org("Mine", "2026-05-05")], [cat("Theirs", "2026-01-01")],
    );
    expect(rows[0].name).toBe("Mine");
    expect(rows[0]._readonly).toBeUndefined();
    expect(rows[1]._readonly).toBe(true);
  });

  it("two catalog entries on one date both survive", () => {
    // Gandhi Jayanti and Dussehra both fell on 2025-10-02.
    const { rows } = mergeHolidayRows([], [
      cat("Gandhi Jayanti", "2025-10-02"), cat("Dussehra", "2025-10-02"),
    ]);
    expect(rows).toHaveLength(2);
  });

  it("no country selected means no preview at all", () => {
    const { rows, preview } = mergeHolidayRows([org("A", "2026-01-01")], []);
    expect(rows).toHaveLength(1);
    expect(preview).toHaveLength(0);
  });

  it("a failed catalog lookup leaves the organization's rows intact", () => {
    // The provider failing must never empty the page.
    const own = [org("A", "2026-01-01"), org("B", "2026-02-01")];
    expect(mergeHolidayRows(own, []).rows).toHaveLength(2);
    expect(mergeHolidayRows(own, null).rows).toHaveLength(2);
  });

  it("tolerates junk input", () => {
    expect(mergeHolidayRows(null, undefined).rows).toEqual([]);
  });

  it("holidayKey distinguishes name and date", () => {
    expect(holidayKey({ date: "2026-01-01", name: "A" }))
      .not.toBe(holidayKey({ date: "2026-01-01", name: "B" }));
    expect(holidayKey({ date: "2026-01-01", name: "A" }))
      .not.toBe(holidayKey({ date: "2026-01-02", name: "A" }));
  });
});

// ── The error trap ───────────────────────────────────────────────────────────
describe("an error is a banner, not a wall", () => {
  it("content still renders while an error is showing", () => {
    const state = holidayPageState({
      loading: false,
      error: { kind: "unsupported_country" },
      rows: [org("Mine", "2026-01-01")],
    });
    expect(state.showBanner).toBe(true);
    expect(state.showContent).toBe(true);
    expect(state.mode).toBe("content");
  });

  it("the organization's holidays are visible even when the provider failed", () => {
    const { rows } = mergeHolidayRows([org("Mine", "2026-01-01")], []);
    const state = holidayPageState({
      loading: false, error: { kind: "provider_unavailable" }, rows,
    });
    expect(state.showContent).toBe(true);
    expect(rows).toHaveLength(1);
  });

  it("loading wins over everything", () => {
    const state = holidayPageState({ loading: true, error: null, rows: [] });
    expect(state.mode).toBe("loading");
    expect(state.showContent).toBe(false);
  });

  it("no rows and no error is the empty state, not an error state", () => {
    const state = holidayPageState({ loading: false, error: null, rows: [] });
    expect(state.mode).toBe("empty");
    expect(state.showBanner).toBe(false);
  });

  it("empty-with-an-error shows BOTH — 'nothing found' and 'why'", () => {
    const state = holidayPageState({
      loading: false, error: { kind: "unsupported_country" }, rows: [],
    });
    expect(state.mode).toBe("empty");
    expect(state.showBanner).toBe(true);
  });
});

// ── Recovery ─────────────────────────────────────────────────────────────────
describe("every selection change clears the previous failure", () => {
  it("selecting a country resets the error before fetching", () => {
    const patch = selectionResetPatch({ countryCode: "US", year: 2026 });
    expect(patch.holidayCountryError).toBeNull();
    expect(patch.holidayCountryRows).toEqual([]);
    expect(patch.shouldFetchCatalog).toBe(true);
  });

  it("CLEARING the country also resets the error — the original trap", () => {
    // Previously this path returned early and left the red banner up, so the
    // whole page stayed hidden until a browser reload.
    const patch = selectionResetPatch({ countryCode: "", year: 2026 });
    expect(patch.holidayCountryError).toBeNull();
    expect(patch.holidayCatalogInfo).toBeNull();
    expect(patch.shouldFetchCatalog).toBe(false);
    expect(patch.shouldFetchOrgHolidays).toBe(true);
  });

  it("changing only the year re-reads both sides", () => {
    const patch = selectionResetPatch({ countryCode: "IN", year: 2027 });
    expect(patch.shouldFetchOrgHolidays).toBe(true);
    expect(patch.shouldFetchCatalog).toBe(true);
  });

  it("stale preview rows never survive a selection change", () => {
    expect(selectionResetPatch({ countryCode: "GB", year: 2026 }).holidayCountryRows)
      .toEqual([]);
  });
});

describe("Retry is offered only where retrying could work", () => {
  it("a provider outage is retryable", () => {
    expect(isRetryableHolidayError("provider_unavailable")).toBe(true);
  });
  it("a network failure is retryable", () => {
    expect(isRetryableHolidayError("network_error")).toBe(true);
  });
  it("an unsupported country is NOT retryable", () => {
    expect(isRetryableHolidayError("unsupported_country")).toBe(false);
  });
  it("a bad year is NOT retryable", () => {
    expect(isRetryableHolidayError("invalid_year")).toBe(false);
  });
});

// ── Upcoming card ────────────────────────────────────────────────────────────
describe("upcoming holidays", () => {
  const rows = [
    org("Past", "2026-01-01"),
    org("Today", "2026-06-15"),
    org("Soon", "2026-07-04"),
    org("Later", "2026-12-25"),
  ];

  it("excludes the past and includes today", () => {
    const out = upcomingHolidays(rows, "2026-06-15");
    expect(out.map(h => h.name)).toEqual(["Today", "Soon", "Later"]);
  });

  it("is sorted soonest first and limited", () => {
    const out = upcomingHolidays(rows, "2026-01-01", 2);
    expect(out.map(h => h.name)).toEqual(["Past", "Today"]);
  });

  it("keeps both holidays that share a date", () => {
    const out = upcomingHolidays(
      [org("Gandhi Jayanti", "2026-10-02"), org("Dussehra", "2026-10-02")],
      "2026-01-01",
    );
    expect(out).toHaveLength(2);
  });

  it("tolerates junk", () => {
    expect(upcomingHolidays(null, "2026-01-01")).toEqual([]);
    expect(upcomingHolidays([{ name: "no date" }], "2026-01-01")).toEqual([]);
  });
});
