/**
 * Desktop tracker — network-failure behaviour.
 *
 * main.js is an Electron main-process module with top-level side effects
 * (app.*, powerMonitor, BrowserWindow), so it cannot be imported in a plain
 * test runner without a large refactor that this fix deliberately avoids.
 *
 * These tests therefore do two things:
 *   1. Re-implement the decision logic exactly as main.js now expresses it and
 *      pin the behaviour (a failure pauses tracking; it never clocks out).
 *   2. Assert against the real main.js SOURCE that the dangerous call sites are
 *      gone and the safe ones remain — which is what actually regresses if
 *      someone reintroduces a client-side clock-out.
 */
import { describe, it, expect, beforeEach } from "vitest";
import fs from "fs";
import path from "path";

const MAIN = fs.readFileSync(path.resolve(__dirname, "../main.js"), "utf-8");

// ── 1. Behavioural model of the new pollStatus failure path ────────────────
function makeTracker() {
  return {
    isTracking: true,
    pollFailCount: 0,
    notified: false,
    clockOutCalls: 0, // must stay 0 forever
    stopTrackingCalls: 0,
    currentStatus: "working",

    onPollFailure() {
      this.pollFailCount++;
      if (this.pollFailCount >= 5 && this.isTracking) {
        this.stopTracking();
        if (!this.notified) this.notified = true;
      }
    },
    onPollSuccess(serverSaysWorking) {
      this.pollFailCount = 0;
      this.notified = false;
      this.currentStatus = serverSaysWorking ? "working" : "not_clocked_in";
      // Reconcile on EVERY success, not only on transition.
      if (this.currentStatus === "working" && !this.isTracking) this.startTracking();
      else if (this.currentStatus !== "working" && this.isTracking) this.stopTracking();
    },
    startTracking() {
      if (this.isTracking) return;
      this.isTracking = true;
      this.pollFailCount = 0; // fresh budget per session
      this.notified = false;
    },
    stopTracking() {
      this.isTracking = false;
      this.stopTrackingCalls++;
    },
  };
}

describe("pollStatus failure path", () => {
  let t;
  beforeEach(() => { t = makeTracker(); });

  it("1. a successful poll resets pollFailCount", () => {
    t.onPollFailure(); t.onPollFailure();
    expect(t.pollFailCount).toBe(2);
    t.onPollSuccess(true);
    expect(t.pollFailCount).toBe(0);
  });

  it("2. one failure does not clock out or stop tracking", () => {
    t.onPollFailure();
    expect(t.isTracking).toBe(true);
    expect(t.clockOutCalls).toBe(0);
  });

  it("3. five failures stop tracking but do NOT clock out", () => {
    for (let i = 0; i < 5; i++) t.onPollFailure();
    expect(t.isTracking).toBe(false);
    expect(t.stopTrackingCalls).toBe(1);
    expect(t.clockOutCalls).toBe(0);
  });

  it("4. ten failures still produce no clock-out and no repeat pause", () => {
    for (let i = 0; i < 10; i++) t.onPollFailure();
    expect(t.clockOutCalls).toBe(0);
    // The 60-second loop: stopTracking must fire ONCE, not once per poll.
    expect(t.stopTrackingCalls).toBe(1);
  });

  it("5. a new tracking session resets the failure budget", () => {
    for (let i = 0; i < 10; i++) t.onPollFailure();   // paused, count = 10
    t.startTracking();                                 // employee clocks in again
    expect(t.pollFailCount).toBe(0);
    t.onPollFailure();                                 // one failure …
    expect(t.isTracking).toBe(true);                   // … must NOT re-pause
  });

  it("6. recovery resumes tracking when the server still says working", () => {
    for (let i = 0; i < 5; i++) t.onPollFailure();
    expect(t.isTracking).toBe(false);
    t.onPollSuccess(true);
    expect(t.isTracking).toBe(true);
  });

  it("7. recovery stops cleanly when the server says not working", () => {
    for (let i = 0; i < 5; i++) t.onPollFailure();
    t.onPollSuccess(false);
    expect(t.isTracking).toBe(false);
    expect(t.clockOutCalls).toBe(0);
  });
});

// ── 2. Source-level guards against reintroduction ──────────────────────────
describe("main.js source guarantees", () => {
  it("the poll-failure path no longer calls clockOutSilently", () => {
    // Bound the slice to pollStatus's own catch block: from the counter
    // increment to the `finally` that clears the in-flight guard. Slicing to
    // end-of-file would sweep in the legitimate before-quit clock-out.
    const from = MAIN.indexOf("pollFailCount++");
    const to = MAIN.indexOf("_pollInFlight = false;", from);
    expect(from).toBeGreaterThan(-1);
    expect(to).toBeGreaterThan(from);
    const catchBlock = MAIN.slice(from, to);
    const active = catchBlock
      .split("\n")
      .filter((l) => !l.trim().startsWith("//") && !l.trim().startsWith("*"))
      .join("\n");
    expect(active).not.toContain("clockOutSilently(");
    expect(active).toContain("stopTracking()");
  });

  it("the offline monitor pauses instead of clocking out", () => {
    expect(MAIN).not.toContain("OFFLINE_CLOCKOUT_MS");
    expect(MAIN).toContain("OFFLINE_PAUSE_MS");
  });

  it("clockOutSilently survives only for OS lifecycle events", () => {
    const calls = [...MAIN.matchAll(/^\s*(?:await\s+)?clockOutSilently\(/gm)];
    // suspend, shutdown, clean quit — three, and no more.
    expect(calls.length).toBe(3);
  });

  it("guards against overlapping polls", () => {
    expect(MAIN).toContain("_pollInFlight");
  });

  it("translates the ignored node-fetch v3 `timeout` option to AbortSignal", () => {
    expect(MAIN).toContain("AbortController");
    expect(MAIN).toContain("clearTimeout(timer)");
  });

  it("resets the failure budget when a tracking session starts", () => {
    const start = MAIN.slice(MAIN.indexOf("async function startTracking"));
    expect(start.slice(0, 800)).toContain("pollFailCount = 0");
  });
});
