/**
 * Adoption view logic — lib/holidayView.
 *
 * The browser used to decide which catalog entries the organization already
 * had, by de-duplicating on date+name. The server decided the same question on
 * (country, date, name), and every pre-migration holiday carries a NULL
 * country, so the two disagreed: the page offered "13 available to add" and the
 * server wrote 20 rows. These pin that the browser no longer decides — it
 * renders the server's classification.
 */
import { describe, it, expect } from "vitest";
import {
  mergeHolidayRows, adoptionCounts, adoptRowState, ADOPT_STATUS,
} from "../lib/holidayView";

const org = (name, date) => ({ id: `${name}`, name, date });
const plan = (name, date, status, extra = {}) => ({
  key: `${date}|${name.toLowerCase()}`,
  name, date, status, eligible: true, holiday_type: "Public",
  reason: "", existing: null, ...extra,
});

// ── Merging uses the server's status, not a local guess ──────────────────────
describe("the preview is built from the server's classification", () => {
  it("only genuinely-new entries are addable", () => {
    const { addable } = mergeHolidayRows([], [
      plan("Diwali", "2026-11-08", ADOPT_STATUS.NEW),
      plan("Christmas", "2026-12-25", ADOPT_STATUS.EXACT_DUPLICATE),
      plan("Gandhi Jayanti", "2026-10-02", ADOPT_STATUS.ALREADY_COVERED),
      plan("Bakrid", "2026-05-27", ADOPT_STATUS.DATE_CONFLICT),
    ]);
    expect(addable.map((e) => e.name)).toEqual(["Diwali"]);
  });

  it("entries the organization already has are not re-listed", () => {
    // Its own row is already in the table with real Edit/Delete actions.
    const { rows } = mergeHolidayRows([org("Christmas Day", "2026-12-25")], [
      plan("Christmas", "2026-12-25", ADOPT_STATUS.EXACT_DUPLICATE),
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]._readonly).toBeUndefined();
  });

  it("conflicts are surfaced separately from addable rows", () => {
    const { addable, conflicts } = mergeHolidayRows([], [
      plan("Diwali", "2026-11-08", ADOPT_STATUS.NEW),
      plan("Bakrid", "2026-05-27", ADOPT_STATUS.DATE_CONFLICT),
    ]);
    expect(addable).toHaveLength(1);
    expect(conflicts).toHaveLength(1);
    expect(conflicts[0].name).toBe("Bakrid");
  });

  it("a conflict is never part of the addable set", () => {
    const { addable } = mergeHolidayRows([], [
      plan("Bakrid", "2026-05-27", ADOPT_STATUS.DATE_CONFLICT),
    ]);
    expect(addable).toHaveLength(0);
  });

  it("ineligible new rows are separated from addable ones", () => {
    const { addable, blocked } = mergeHolidayRows([], [
      plan("Ganesh Chaturthi", "2026-09-14", ADOPT_STATUS.NEW,
        { eligible: false, holiday_type: "Optional" }),
      plan("Diwali", "2026-11-08", ADOPT_STATUS.NEW),
    ]);
    expect(addable.map((e) => e.name)).toEqual(["Diwali"]);
    expect(blocked.map((e) => e.name)).toEqual(["Ganesh Chaturthi"]);
  });

  it("organization rows come first and stay editable", () => {
    const { rows } = mergeHolidayRows([org("Mine", "2026-01-01")], [
      plan("Theirs", "2026-06-06", ADOPT_STATUS.NEW),
    ]);
    expect(rows[0].name).toBe("Mine");
    expect(rows[0]._readonly).toBeUndefined();
    expect(rows[1]._readonly).toBe(true);
  });

  it("no plan means no preview — the org's rows stand alone", () => {
    const { rows, addable, conflicts } = mergeHolidayRows(
      [org("A", "2026-01-01")], []);
    expect(rows).toHaveLength(1);
    expect(addable).toHaveLength(0);
    expect(conflicts).toHaveLength(0);
  });

  it("a failed plan lookup leaves the org's rows intact", () => {
    expect(mergeHolidayRows([org("A", "2026-01-01")], null).rows).toHaveLength(1);
    expect(mergeHolidayRows(null, undefined).rows).toEqual([]);
  });

  it("every preview row keeps its server key for per-row actions", () => {
    const { preview } = mergeHolidayRows([], [
      plan("Diwali", "2026-11-08", ADOPT_STATUS.NEW),
    ]);
    expect(preview[0].key).toBe("2026-11-08|diwali");
  });
});

// ── Counts come from the server ──────────────────────────────────────────────
describe("header counts are the server's, never re-derived", () => {
  it("reads will_add rather than counting rows", () => {
    const c = adoptionCounts({
      counts: {
        will_add: 9, new: 9, already_covered: 2, exact_duplicate: 10,
        date_conflict: 1, not_eligible: 2, total: 22,
      },
    });
    expect(c).toEqual({
      new: 9, alreadyCovered: 2, exactDuplicates: 10,
      conflicts: 1, notEligible: 2, total: 22,
    });
  });

  it("org 1's real breakdown adds up to the catalog size", () => {
    const c = adoptionCounts({
      counts: {
        will_add: 9, new: 9, already_covered: 2, exact_duplicate: 10,
        date_conflict: 1, not_eligible: 2, total: 22,
      },
    });
    expect(c.new + c.alreadyCovered + c.exactDuplicates + c.conflicts).toBe(c.total);
  });

  it("a missing plan reads as zeroes, not NaN", () => {
    const c = adoptionCounts(null);
    expect(Object.values(c).every((v) => v === 0)).toBe(true);
  });
});

// ── Per-row action ───────────────────────────────────────────────────────────
describe("per-row action matches the row's status", () => {
  it("a new eligible row offers Add", () => {
    const s = adoptRowState({ _readonly: true, status: ADOPT_STATUS.NEW, eligible: true });
    expect(s.action).toBe("add");
    expect(s.label).toBe("Add");
  });

  it("a conflict offers Review, never Add", () => {
    const s = adoptRowState({ _readonly: true, status: ADOPT_STATUS.DATE_CONFLICT });
    expect(s.action).toBe("review");
    expect(s.label).toBe("Review conflict");
  });

  it("an exact duplicate offers nothing", () => {
    const s = adoptRowState({ _readonly: true, status: ADOPT_STATUS.EXACT_DUPLICATE });
    expect(s.action).toBeNull();
    expect(s.label).toBe("Already covered");
  });

  it("an already-covered row offers nothing", () => {
    const s = adoptRowState({ _readonly: true, status: ADOPT_STATUS.ALREADY_COVERED });
    expect(s.action).toBeNull();
  });

  it("an ineligible row offers nothing and says so", () => {
    const s = adoptRowState({ _readonly: true, status: ADOPT_STATUS.NEW, eligible: false });
    expect(s.action).toBeNull();
    expect(s.label).toBe("Not included");
  });

  it("the organization's own rows are not preview rows at all", () => {
    expect(adoptRowState({ name: "Mine" }).action).toBeNull();
    expect(adoptRowState(null).action).toBeNull();
  });

  it("Add is never offered for anything already present", () => {
    for (const status of [ADOPT_STATUS.EXACT_DUPLICATE, ADOPT_STATUS.ALREADY_COVERED,
                          ADOPT_STATUS.DATE_CONFLICT]) {
      expect(adoptRowState({ _readonly: true, status }).action).not.toBe("add");
    }
  });
});
