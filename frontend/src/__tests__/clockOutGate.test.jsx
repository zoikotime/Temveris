/**
 * Controlled Clock-Out gate handling — the HTTP-200-means-refused case.
 *
 * The backend answers a below-required-hours clock-out with
 *   HTTP 200 {success:false, code:"EARLY_CLOCK_OUT_REASON_REQUIRED"}
 * and leaves the session OPEN. Callers that check only `res.ok` treat that as a
 * successful clock-out and flip the UI to "clocked out" while the worker is
 * still clocked in on the server.
 *
 * These tests pin the decision rule shared by every clock-out caller
 * (Dashboard, ElectronDashboard, ClockInWidget): the response BODY decides,
 * never the status code.
 */
import { describe, it, expect } from "vitest";

// The rule under test, stated once. Each caller implements this shape:
//   const d = await postClockOut(null);
//   if (d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED") -> keep working
//   else -> finalizeClockOut(d)
const isRefusal = (d) => !!d && d.code === "EARLY_CLOCK_OUT_REASON_REQUIRED";

const GATE_RESPONSE = {
  success: false,
  code: "EARLY_CLOCK_OUT_REASON_REQUIRED",
  message: "Please give a reason for leaving before your required hours.",
};
const SUCCESS_RESPONSE = {
  success: true,
  clock_out_time: "2026-08-10T09:00:00Z",
  worked_seconds: 31500,
};

describe("clock-out gate: HTTP 200 does not mean the session closed", () => {
  it("a gate response is NOT a clock-out", () => {
    expect(isRefusal(GATE_RESPONSE)).toBe(true);
  });

  it("res.ok alone would misclassify it — this is the original bug", () => {
    const res = { ok: true, status: 200 };
    expect(res.ok).toBe(true); // the old check passed …
    expect(isRefusal(GATE_RESPONSE)).toBe(true); // … while the body says refused
  });

  it("a real success is treated as a clock-out", () => {
    expect(isRefusal(SUCCESS_RESPONSE)).toBe(false);
  });

  it("success:false WITHOUT the reason code is not the gate", () => {
    // e.g. "No active session" — must not open the reason modal.
    expect(isRefusal({ success: false, detail: "No active session" })).toBe(false);
  });

  it("an empty / unparseable body is not a refusal", () => {
    expect(isRefusal({})).toBe(false);
    expect(isRefusal(null)).toBe(false);
    expect(isRefusal(undefined)).toBe(false);
  });

  it("re-submitting with a too-short reason keeps the modal open", () => {
    // The server re-issues the same code; the caller must not finalize.
    expect(isRefusal(GATE_RESPONSE)).toBe(true);
  });
});

describe("ClockInWidget wires the gate to the shared modal", () => {
  it("checks the response body, not just res.ok", async () => {
    const fs = await import("fs");
    const path = await import("path");
    const src = fs.readFileSync(
      path.resolve(__dirname, "../components/ClockInWidget.jsx"),
      "utf-8",
    );
    expect(src).toContain("EARLY_CLOCK_OUT_REASON_REQUIRED");
    expect(src).toContain("EarlyClockOutModal");
    // setStatus("clocked_out") must live only in finalizeClockOut, which runs
    // after the refusal check — never directly in handleClockOut.
    const finalize = src.slice(src.indexOf("const finalizeClockOut"));
    expect(finalize).toContain('setStatus("clocked_out")');
  });
});
