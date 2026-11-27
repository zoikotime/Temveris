/**
 * A failed /employees/team refresh must never pass old data off as current.
 *
 * The dashboard hydrates its worker grid from a localStorage snapshot and then
 * revalidates over the network. Both failure paths used to be silent: the
 * request either threw into a bare `catch {}`, or came back as a non-array
 * error body that the `Array.isArray` guard quietly skipped. Either way
 * `employees` was simply never replaced, so the previous snapshot stayed on
 * screen looking live — which is how a days-old grid rendered with no
 * indication anything was wrong.
 *
 * WHY THESE ARE STRUCTURAL TESTS
 * ------------------------------
 * The properties below are all reachable by mounting AdminDashboard, and that
 * was tried first. It is not viable here: the page pulls four contexts (two of
 * which throw without a provider), opens a WebSocket, runs polling intervals,
 * and — since the deferred-preload change — fires thirteen dynamic chunk
 * imports including recharts. A harness for that is a new test architecture,
 * not a regression test for this fix.
 *
 * So these assert on the source, in the same idiom the repository already uses
 * for this page (see serverArrivalStatus.test.jsx). They are deliberately
 * narrow: each one pins a decision that, if silently reverted, restores the
 * exact "stale data looks live" bug. They do NOT claim to prove rendering.
 */
import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

const SRC = path.resolve(__dirname, "..");
const DASH = fs.readFileSync(path.join(SRC, "pages/AdminDashboard.jsx"), "utf8");

/** The body of a top-level `const <name> = useCallback(async () => {` block. */
function fnBody(src, name) {
  const start = src.indexOf(`const ${name} = useCallback(`);
  expect(start, `${name} not found — was it renamed?`).toBeGreaterThan(-1);
  // Ends at the callback's dependency array, which every one of these has.
  const end = src.indexOf("}, [", start);
  expect(end).toBeGreaterThan(start);
  return src.slice(start, end);
}

const fetchEmployees = fnBody(DASH, "fetchEmployees");
const retryEmployees = fnBody(DASH, "retryEmployees");

// ── The failure paths are no longer silent ──────────────────────────────────

describe("fetchEmployees marks a failed refresh", () => {
  it("does not swallow a thrown request", () => {
    // The original was literally `} catch { }`.
    expect(/catch\s*\{\s*\}/.test(fetchEmployees)).toBe(false);
    expect(fetchEmployees).toMatch(/catch\s*\{[\s\S]*setEmployeesLoadFailed\(true\)/);
  });

  it("treats a non-array body as a failure, not as 'no update'", () => {
    // apiFetch resolves the RESPONSE BODY on a non-2xx, so a 500 arrives as an
    // object. Skipping it silently is the second half of the original bug.
    expect(fetchEmployees).toMatch(
      /if\s*\(!Array\.isArray\(data\)\)\s*\{[\s\S]*setEmployeesLoadFailed\(true\)[\s\S]*return;/);
  });

  it("clears the flag on a good payload", () => {
    expect(fetchEmployees).toMatch(/setEmployeesLoadFailed\(false\)/);
  });

  it("clears it on the /team payload alone, before awaiting early-clock-outs", () => {
    // Ordering matters: if the clear happened after `await ecoPromise`, a slow
    // or failing supplementary call would hold the stale banner up over data
    // that is actually current.
    const cleared = fetchEmployees.indexOf("setEmployeesLoadFailed(false)");
    const ecoAwait = fetchEmployees.indexOf("await ecoPromise");
    expect(cleared).toBeGreaterThan(-1);
    expect(ecoAwait).toBeGreaterThan(-1);
    expect(cleared).toBeLessThan(ecoAwait);
  });

  it("never lets the early-clock-outs call reject into the team path", () => {
    expect(fetchEmployees).toMatch(/early-clock-outs[\s\S]{0,120}\.catch\(/);
  });
});

// ── The last known-good cache survives a failure ────────────────────────────

describe("localStorage cache", () => {
  it("is only written inside the successful branch", () => {
    const guard = fetchEmployees.indexOf("if (!Array.isArray(data))");
    const write = fetchEmployees.indexOf("localStorage.setItem");
    expect(guard).toBeGreaterThan(-1);
    expect(write).toBeGreaterThan(guard);   // unreachable once the guard returns
  });

  it("never clears or blanks the cache on failure", () => {
    expect(/localStorage\.removeItem/.test(fetchEmployees)).toBe(false);
    expect(/setEmployees\(\[\]\)/.test(fetchEmployees)).toBe(false);
  });
});

// ── WebSocket safety ────────────────────────────────────────────────────────

describe("websocket patches", () => {
  it("keeps the functional update when folding in early-clock-outs", () => {
    // A plain snapshot write here would clobber any status patch that landed
    // while the request was in flight.
    expect(fetchEmployees).toMatch(/setEmployees\(prev\s*=>/);
  });
});

// ── Retry ───────────────────────────────────────────────────────────────────

describe("retryEmployees", () => {
  it("delegates to the one fetch function instead of re-issuing the request", () => {
    expect(retryEmployees).toMatch(/fetchEmployees\(\)/);
    expect(/apiFetch\(/.test(retryEmployees)).toBe(false);
  });

  it("refuses to re-enter while a retry is already running", () => {
    expect(retryEmployees).toMatch(/if\s*\(employeesRetrying\)\s*return;/);
  });

  it("does not touch `loading`, which would blank the data being preserved", () => {
    expect(/setLoading\(/.test(retryEmployees)).toBe(false);
  });
});

// ── The notice itself ───────────────────────────────────────────────────────

describe("the stale-data notice", () => {
  const banner = (() => {
    const start = DASH.indexOf("{employeesLoadFailed && (");
    expect(start, "the notice block was not found").toBeGreaterThan(-1);
    return DASH.slice(start, start + 2600);
  })();

  it("distinguishes stale data from no data at all", () => {
    expect(banner).toMatch(/employees\.length\s*>\s*0/);
    expect(banner).toMatch(/employeeDataStale/);
    expect(banner).toMatch(/employeeDataUnavailable/);
  });

  it("renders above the tab switch so every worker view shows it", () => {
    const notice = DASH.indexOf("{employeesLoadFailed && (");
    const firstTab = DASH.indexOf('{tab === "overview" && (');
    expect(firstTab).toBeGreaterThan(-1);
    expect(notice).toBeLessThan(firstTab);
  });

  it("is announced and operable, not colour-only", () => {
    expect(banner).toMatch(/role="status"/);
    expect(banner).toMatch(/aria-live="polite"/);
    expect(banner).toMatch(/<button/);
    expect(banner).toMatch(/type="button"/);
    expect(banner).toMatch(/aria-hidden="true"/);   // the icon is decorative
  });

  it("disables Retry while a retry is in flight", () => {
    expect(banner).toMatch(/disabled=\{employeesRetrying\}/);
  });

  it("leaks no server error detail to the user", () => {
    // No status codes, response bodies or exception text in the notice.
    for (const leak of ["err.", "error.message", "res.status", "response.data", ".detail"]) {
      expect(banner.includes(leak), `notice must not surface ${leak}`).toBe(false);
    }
  });
});
