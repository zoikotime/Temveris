/**
 * The Organization Timezone control may only offer real IANA zones.
 *
 * One production workspace held the free text "California, Sacramento". It is
 * not a zone, so it silently resolved to the system fallback — the settings
 * screen showed a US city while every attendance decision for that tenant was
 * made on another clock, and the row was captioned "Admin & report display" so
 * nobody reviewing it believed it mattered.
 *
 * The field is now a searchable picker backed by the browser's own tz database,
 * and it refuses to save anything the backend would reject. These tests pin the
 * list and the validation; the backend's ZoneInfo check remains the authority.
 */
import { describe, it, expect } from "vitest";
import { groupedIanaZones, isSelectableZone, listIanaZones } from "../lib/orgTime";
import { EN_STRINGS as EN } from "../context/enStrings";

const ZONES = listIanaZones();

describe("Organization Timezone picker", () => {
  it("offers the browser's IANA zone database", () => {
    expect(Array.isArray(ZONES)).toBe(true);
    // Even the curated fallback list clears this bar; a real tz database has ~450.
    expect(ZONES.length).toBeGreaterThan(15);
  });

  it.each([
    "Asia/Kolkata",
    "America/Los_Angeles",
    "America/New_York",
    "Europe/London",
    "Europe/Berlin",
    "UTC",
  ])("offers %s", (zone) => {
    expect(ZONES).toContain(zone);
  });

  it("offers only values the validator accepts", () => {
    const rejected = ZONES.filter((z) => !isSelectableZone(z));
    expect(rejected).toEqual([]);
  });

  it("contains no free-text or offset entries", () => {
    const bad = ZONES.filter((z) => z.includes(",") || z.startsWith("GMT+") || z.startsWith("+"));
    expect(bad).toEqual([]);
  });
});

describe("Organization Timezone dropdown grouping", () => {
  const GROUPS = groupedIanaZones();

  it("groups every offered zone, losing none", () => {
    const flattened = GROUPS.flatMap((g) => g.zones);
    expect(flattened.length).toBe(ZONES.length);
    expect([...flattened].sort()).toEqual([...ZONES].sort());
  });

  it("names each group after the region prefix", () => {
    for (const { region, zones } of GROUPS) {
      for (const z of zones) {
        const expected = z.includes("/") ? z.split("/")[0] : "Other";
        expect(expected).toBe(region);
      }
    }
  });

  it("offers the regions an admin expects, in alphabetical order", () => {
    const names = GROUPS.map((g) => g.region);
    expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b)));
    for (const region of ["Africa", "America", "Asia", "Europe"]) {
      expect(names).toContain(region);
    }
  });

  it("keeps UTC reachable even though it has no region prefix", () => {
    const other = GROUPS.find((g) => g.region === "Other");
    expect(other?.zones).toContain("UTC");
  });
});

describe("Organization Timezone validation", () => {
  it.each([
    "Asia/Kolkata",
    "America/Los_Angeles",
    "America/New_York",
    "Europe/London",
    "Europe/Berlin",
    "UTC",
  ])("accepts %s", (zone) => {
    expect(isSelectableZone(zone)).toBe(true);
  });

  it.each([
    "California, Sacramento",  // the exact production value
    "IST",
    "GMT+5.5",
    "+05:30",
    "India",
    "random text",
    "",
    "   ",
    null,
    undefined,
  ])("rejects %p", (value) => {
    expect(isSelectableZone(value)).toBe(false);
  });

  it("trims before validating, so a pasted value with spaces still saves", () => {
    expect(isSelectableZone("  Europe/London  ")).toBe(true);
  });
});

describe("Organization Timezone wording", () => {
  it("is named for the organization, not for display", () => {
    expect(EN.organizationTimezone).toBe("Organization Timezone");
    expect(EN.organizationTimezone.toLowerCase()).not.toContain("display");
  });

  it("states every system the clock governs", () => {
    const hint = EN.organizationTimezoneHint.toLowerCase();
    for (const system of ["attendance", "absence", "shift", "report",
                          "payroll", "day boundaries"]) {
      expect(hint).toContain(system);
    }
  });

  it("says the timezone covers the whole organization", () => {
    const hint = EN.organizationTimezoneHint.toLowerCase();
    expect(hint).toContain("organization");
    expect(hint).toContain("every employee");
  });

  it("no longer captions the row as display-only", () => {
    expect(EN.organizationTimezoneUse.toLowerCase()).not.toContain("display");
  });
});
