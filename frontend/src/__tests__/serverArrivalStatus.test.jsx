/**
 * P9 — the client renders server attendance values and computes none of its own.
 *
 * Before P9 the dashboard derived Late / Absent from `default_clock_in_time` with
 * a hardcoded Sat/Sun weekend and a `?? 20` grace default, and
 * frontend/src/lib/attendancePolicy.js duplicated the 8h/4h expected-hours table.
 * Both silently disagreed with the backend as soon as an org configured anything.
 *
 * These tests defend three things:
 *   1. computeShiftInfo is a LOOKUP over the server's `arrival_status`
 *   2. it performs no date / grace / threshold arithmetic — proven by feeding it
 *      only a server status and no legacy inputs, and by feeding it legacy inputs
 *      with no server status and getting nothing back
 *   3. the duplicate policy module and its client-side constants are gone
 */
import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

import { computeShiftInfo, STATUS_CONFIG } from "../components/admin/adminShared";

const SRC = path.resolve(__dirname, "..");
const read = (p) => fs.readFileSync(path.join(SRC, p), "utf8");

// ── 1. arrival_status drives the phase ──────────────────────────────────────

describe("computeShiftInfo maps the server arrival_status", () => {
  const cases = [
    ["present",        "within_grace"],
    ["awaiting",       "before_shift"],
    ["late",           "late_present"],
    ["very_late",      "very_late"],
    ["late_pending",   "late_pending"],
    ["absent",         "absent_after_noon"],
  ];
  it.each(cases)("%s -> %s", (arrival, phase) => {
    const r = computeShiftInfo({ arrival_status: arrival });
    expect(r.phase).toBe(phase);
    expect(r.hasShift).toBe(true);
    expect(r.arrivalStatus).toBe(arrival);
  });

  it("not_applicable means no shift", () => {
    const r = computeShiftInfo({ arrival_status: "not_applicable" });
    expect(r.hasShift).toBe(false);
    expect(r.phase).toBe("no_shift");
  });

  it("renders late_minutes from the server, not from a local clock", () => {
    const r = computeShiftInfo({ arrival_status: "late", late_minutes: 37 });
    expect(r.lateMinutes).toBe(37);
    expect(r.lateLabel).toBe("Late by 37 min");
  });

  it("formats an hour-plus delay", () => {
    const r = computeShiftInfo({ arrival_status: "very_late", late_minutes: 125 });
    expect(r.lateLabel).toBe("Late by 2 hr 5 min");
  });

  it("exposes the server shift start for display only", () => {
    const r = computeShiftInfo({ arrival_status: "present", shift_start_time: "10:30" });
    expect(r.shiftStart).toBe("10:30");
    // graceEnd was a client-computed Date; it must no longer exist.
    expect(r.graceEnd).toBeNull();
  });
});

// ── 2. no client-side derivation remains ────────────────────────────────────

describe("the client computes nothing itself", () => {
  it("ignores the legacy fields when the server sent no arrival_status", () => {
    // Pre-P9 this input alone produced a full phase/late computation. It must now
    // yield "no shift" rather than falling back to a second implementation.
    const r = computeShiftInfo({
      default_clock_in_time: "09:00",
      default_clock_in_grace_min: 20,
      clock_in_time: new Date().toISOString(),
      status: "Working",
    });
    expect(r.hasShift).toBe(false);
    expect(r.phase).toBe("no_shift");
    expect(r.lateMinutes).toBeNull();
  });

  it("does not depend on the current time", () => {
    // Same input twice with the clock advanced would have differed pre-P9.
    const emp = { arrival_status: "late", late_minutes: 12 };
    const a = computeShiftInfo(emp);
    const b = computeShiftInfo(emp);
    expect(a).toEqual(b);
    expect(a.lateMinutes).toBe(12);
  });

  it("handles a null employee", () => {
    expect(computeShiftInfo(null).hasShift).toBe(false);
  });

  it("adminShared no longer contains grace or noon-cutoff arithmetic", () => {
    const src = read("components/admin/adminShared.jsx");
    // The specific constructs the old derivation used.
    expect(src).not.toMatch(/default_clock_in_grace_min\s*\)\s*\?/);
    expect(src).not.toMatch(/graceEnd\s*=\s*new Date/);
    expect(src).not.toMatch(/weekday === 0 \|\| weekday === 6/);
    expect(src).not.toMatch(/export const SHIFT_NOON_HOUR/);
    expect(src).not.toMatch(/export const _istNow/);
  });

  it("AdminDashboard performs no expected-hours or lateness maths", () => {
    const src = read("pages/AdminDashboard.jsx");
    expect(src).not.toMatch(/expectedWorkSeconds/);
    expect(src).not.toMatch(/8\s*\*\s*3600/);
    expect(src).not.toMatch(/attendancePolicy/);
  });

  it("the duplicate policy module is deleted", () => {
    expect(fs.existsSync(path.join(SRC, "lib/attendancePolicy.js"))).toBe(false);
  });

  it("nothing imports the deleted module", () => {
    const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap(e => {
      const p = path.join(dir, e.name);
      return e.isDirectory() ? walk(p) : [p];
    });
    const offenders = walk(SRC)
      .filter(f => /\.jsx?$/.test(f))
      // Product code only — these very tests name the removed symbols in order
      // to assert they are gone.
      .filter(f => !f.includes("__tests__"))
      .filter(f => /attendancePolicy|expectedWorkSeconds/.test(fs.readFileSync(f, "utf8")))
      .map(f => path.relative(SRC, f));
    expect(offenders).toEqual([]);
  });

  it("MyTeamTab reads expected_seconds from the row", () => {
    const src = read("components/admin/MyTeamTab.jsx");
    expect(src).toMatch(/expected_seconds/);
    expect(src).not.toMatch(/expectedWorkSeconds/);
  });

  it("Dashboard reads expected_work_seconds from the server", () => {
    const src = read("pages/Dashboard.jsx");
    expect(src).toMatch(/expected_work_seconds/);
    expect(src).not.toMatch(/expectedWorkSeconds\(/);
  });
});

// ── 3. Very Late badge ──────────────────────────────────────────────────────

describe("Very Late badge", () => {
  it("is registered with a distinct label and its own colours", () => {
    expect(STATUS_CONFIG.very_late).toBeTruthy();
    expect(STATUS_CONFIG.very_late.label).toBe("Very Late");
    expect(STATUS_CONFIG.very_late.key).toBe("very_late");
    // Distinct from Late and from Absent, so the three read differently.
    expect(STATUS_CONFIG.very_late.color).not.toBe(STATUS_CONFIG.late.color);
    expect(STATUS_CONFIG.very_late.color).not.toBe(STATUS_CONFIG.absent?.color);
  });

  it("accepts the spaced spelling too, like the other statuses", () => {
    expect(STATUS_CONFIG["very late"].key).toBe("very_late");
  });

  it("carries the same token shape as an existing badge", () => {
    const keys = Object.keys(STATUS_CONFIG.working).sort();
    expect(Object.keys(STATUS_CONFIG.very_late).sort()).toEqual(keys);
  });

  it("Late is registered alongside it", () => {
    expect(STATUS_CONFIG.late.label).toBe("Late");
  });

  // The half-day arrival status was removed with its setting: an arrival past
  // the very-late threshold now stays Very Late, so nothing can emit "half_day".
  it("half_day is gone", () => {
    expect(STATUS_CONFIG.half_day).toBeUndefined();
  });
});
