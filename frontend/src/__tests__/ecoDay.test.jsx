/**
 * WF-ECO-03 — the Worker's ECO v2 day view.
 *
 * The property under test: **the page is a consumer, not a second
 * implementation.** It prints the status and body the backend rendered, shows
 * the numbers the backend sent, and decides nothing itself.
 *
 * The behavioural tests use payloads that carry the real §28.1 labels, so a
 * page that quietly stopped printing them would fail. The static guards at the
 * end then prove those strings are absent from the page's own source — which
 * is what stops the page re-deriving them instead of displaying them.
 */
import React from "react";
import fs from "fs";
import path from "path";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

vi.mock("../components/TopNavbar", () => ({ default: () => <nav /> }));
vi.mock("../context/LanguageContext", () => ({
  useLanguage: () => ({ T: {}, lang: "en" }),
}));
vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false, toggleDark: () => {} }),
}));

const authFetch = vi.fn();
vi.mock("../api/authFetch", () => ({
  authFetch: (...a) => authFetch(...a),
  authFetchJson: vi.fn(),
  default: (...a) => authFetch(...a),
}));

const readMirror = vi.fn();
vi.mock("../api/authStorage", () => ({
  readMirror: (...a) => readMirror(...a),
  readToken: () => "token",
  redirectToLogin: vi.fn(),
}));

import MyWorkingTime from "../pages/MyWorkingTime";
import { todayLocalISO } from "../api/ecoDay";

const SRC = (p) => fs.readFileSync(path.resolve(__dirname, p), "utf8");
const PAGE = SRC("../pages/MyWorkingTime.jsx");
const CLIENT = SRC("../api/ecoDay.js");

/** Source with comments removed.
 *
 *  The static guards below must inspect CODE, not prose. The page's docstring
 *  legitimately explains what it does NOT do — "no mapping from
 *  evaluation_state", "the weekly roll-up is deferred", "qualifying_seconds is
 *  not persisted" — and a guard that reads comments would fail precisely
 *  because the file documents the rule it is obeying. */
const stripComments = (src) =>
  src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");

const PAGE_CODE = stripComments(PAGE);
const CLIENT_CODE = stripComments(CLIENT);

const DEFERRED_BODY =
  "Your exit is still being resolved, so today’s shortfall is not final; " +
  "while this day is deferred, it contributes 0 minutes to your weekly total " +
  "and will be evaluated when the exit is resolved.";

const payload = (over = {}) => ({
  work_date: "2026-09-07",
  status: "Complete",
  body: null,
  has_explanation: false,
  copy_keys: ["eco.status.complete"],
  evaluation_state: "NO_SHORTFALL",
  evaluation_generation: 1,
  ledger_version: 1,
  policy_version: null,
  config_version: "cfg-1",
  leave_input_snapshot_key: "snap-1",
  required_minutes: 480,
  qualifying_minutes: 480,
  gross_shortfall_minutes: 0,
  exclusion_minutes: 0,
  attributable_minutes: 0,
  schedule_variance_minutes: null,
  contributes_to_accumulator_minutes: 0,
  shortfall_computed: true,
  ...over,
});

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const fail = (status) => ({ ok: false, status, json: async () => ({}) });

const lastUrl = () => new URL(authFetch.mock.calls.at(-1)[0], "http://x");

/** Wait for the in-flight load to land. Tests that assert only on the static
 *  chrome still have a pending fetch behind them; without this the resolution
 *  lands after the test ends and React reports an unwrapped act(). */
const settled = () => screen.findByText("2026-09-07");

/**
 * Every term→definition pair on the card, as [label, value].
 *
 * This is the shape the owner ruling turns on. §28's vocabulary genuinely puts
 * "Counted" in two places — a Status value and the `attributable_minutes`
 * column — and neither may be renamed. So the two are told apart by WHAT LABELS
 * THEM, never by tag, position, or the text itself:
 *
 *     ["Status",  "Counted"]      the day's §28.1 Status
 *     ["Counted", "1h 0m"]        the §28 metric
 */
const pairs = () =>
  [...document.querySelectorAll("dt")]
    .map((dt) => [dt.textContent, dt.nextElementSibling?.textContent]);

/** The §28.1 Status value, located through its own "Status" label. */
const statusValue = () =>
  [...document.querySelectorAll("dt")]
    .find((dt) => dt.textContent === "Status")?.nextElementSibling ?? null;

/** Elements whose OWN text is exactly `text`. This is the probe the audit used
 *  to prove the status was an unlabelled bare paragraph; it now proves the
 *  opposite, so it stays. */
const ownTextIs = (text) =>
  [...document.querySelectorAll("*")].filter(
    (el) => [...el.childNodes].filter((n) => n.nodeType === 3)
      .map((n) => n.textContent).join("").trim() === text);

beforeEach(() => {
  vi.clearAllMocks();
  readMirror.mockImplementation((k) => (k === "user_id" ? "8811" : null));
  authFetch.mockResolvedValue(ok(payload()));
});
afterEach(() => vi.clearAllMocks());

// ═══════════════════════════════════════════════════════════════════════════
// Request contract
// ═══════════════════════════════════════════════════════════════════════════
describe("request", () => {
  it("renders the page", async () => {                       // 1
    render(<MyWorkingTime />);
    expect(await screen.findByRole("heading", { level: 1 })).toBeTruthy();
    await settled();
  });

  it("defaults to the LOCAL calendar date, not the UTC one", async () => {  // 2
    // Asserted as a CONTRACT rather than by comparing against toISOString:
    // under a UTC test runner the two agree, so that comparison would pass
    // vacuously and prove nothing. The contract is that the helper reads the
    // LOCAL calendar fields, which holds in every zone.
    const late = new Date(2026, 8, 7, 21, 30, 0);
    expect(todayLocalISO(late)).toBe("2026-09-07");
    const d = new Date(2026, 0, 3, 23, 59, 59);
    expect(todayLocalISO(d)).toBe("2026-01-03");   // zero-padded, local

    // And the UTC construction is absent from the client, which is the part a
    // UTC runner cannot verify behaviourally.
    expect(CLIENT_CODE).not.toContain("toISOString");

    render(<MyWorkingTime />);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    expect(lastUrl().searchParams.get("work_date")).toBe(todayLocalISO());
  });

  it("sends the selected work_date", async () => {           // 3
    render(<MyWorkingTime />);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/date/i),
                     { target: { value: "2026-09-01" } });
    await waitFor(() =>
      expect(lastUrl().searchParams.get("work_date")).toBe("2026-09-01"));
  });

  it("requests the authenticated Worker's own id", async () => {   // 4
    render(<MyWorkingTime />);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    expect(lastUrl().searchParams.get("user_id")).toBe("8811");
    expect(readMirror).toHaveBeenCalledWith("user_id");
  });

  it("sends no organization_id and no audience", async () => {     // 5, 6
    render(<MyWorkingTime />);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    const q = lastUrl().searchParams;
    expect(q.get("organization_id")).toBeNull();
    expect(q.get("audience")).toBeNull();
    expect([...q.keys()].sort()).toEqual(["user_id", "work_date"]);
  });

  it("calls /api/eco/day and nothing else", async () => {          // 21, 22, 23
    render(<MyWorkingTime />);
    await waitFor(() => expect(authFetch).toHaveBeenCalled());
    for (const [url] of authFetch.mock.calls) {
      expect(url).toContain("/api/eco/day");
      for (const legacy of ["/me/day-summary", "/reports/", "/ai/summary",
                            "/tracking/", "/break/"]) {
        expect(url).not.toContain(legacy);
      }
    }
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// Rendering — every value comes off the payload
// ═══════════════════════════════════════════════════════════════════════════
describe("rendering", () => {
  it("renders Deferred status and body exactly as sent", async () => {  // 7, 8
    authFetch.mockResolvedValue(ok(payload({
      status: "Deferred", body: DEFERRED_BODY, has_explanation: true,
      evaluation_state: "DEFERRED_PENDING_LEDGER",
      required_minutes: null, qualifying_minutes: null,
      gross_shortfall_minutes: null, exclusion_minutes: null,
      attributable_minutes: null, contributes_to_accumulator_minutes: 0,
      shortfall_computed: false,
    })));
    render(<MyWorkingTime />);
    expect(await screen.findByText("Deferred")).toBeTruthy();
    expect(await screen.findByText(DEFERRED_BODY)).toBeTruthy();
  });

  it.each([
    ["Complete", "NO_SHORTFALL"],                                     // 9
    ["Counted", "SHORTFALL"],                                         // 10
    ["Not a working day", "NOT_A_WORKING_DAY"],                       // 11
  ])("renders %s from the backend status", async (status, state) => {
    authFetch.mockResolvedValue(ok(payload({ status, evaluation_state: state })));
    render(<MyWorkingTime />);
    await settled();
    expect(statusValue().textContent).toBe(status);
  });

  it("renders a status the page has never seen, verbatim", async () => {
    // The strongest proof there is no mapping table: an unknown label still
    // reaches the screen untouched.
    authFetch.mockResolvedValue(ok(payload({ status: "Zzz Placeholder Status" })));
    render(<MyWorkingTime />);
    expect(await screen.findByText("Zzz Placeholder Status")).toBeTruthy();
  });

  it("invents no label when status is null", async () => {            // 12
    authFetch.mockResolvedValue(ok(payload({
      status: null, body: null, has_explanation: false, copy_keys: [],
      evaluation_state: "REQUIREMENT_UNRESOLVED",
    })));
    render(<MyWorkingTime />);
    expect(await screen.findByText("2026-09-07")).toBeTruthy();
    for (const invented of [/unknown/i, /^pending$/i, /unavailable/i,
                            /^error$/i, /not evaluated/i]) {
      expect(screen.queryByText(invented)).toBeNull();
    }
  });

  it("shows the backend's minute figures and derives none", async () => {
    authFetch.mockResolvedValue(ok(payload({
      status: "Counted", evaluation_state: "SHORTFALL",
      required_minutes: 480, qualifying_minutes: 420,
      gross_shortfall_minutes: 60, exclusion_minutes: 0,
      attributable_minutes: 60,
    })));
    render(<MyWorkingTime />);
    expect(await screen.findByText("8h 0m")).toBeTruthy();   // required 480
    expect(await screen.findByText("7h 0m")).toBeTruthy();   // qualifying 420
    expect((await screen.findAllByText("1h 0m")).length).toBeGreaterThan(0);
  });

  it("does not render provenance identifiers as Worker content", async () => { // 27
    authFetch.mockResolvedValue(ok(payload({
      config_version: "CFGX", leave_input_snapshot_key: "SNAPX",
      policy_version: "POLX",
    })));
    render(<MyWorkingTime />);
    await screen.findByText("2026-09-07");
    for (const id of ["CFGX", "SNAPX", "POLX"]) {
      expect(screen.queryByText(new RegExp(id))).toBeNull();
    }
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// The status field is LABELLED — owner ruling, post-implementation audit
//
// The audit proved the §28.1 Status rendered as a bare <p>: role `paragraph`,
// accessible name "". Nothing said it was the day's Status, so on a shortfall
// day the word "Counted" appeared twice with no way — visual or programmatic —
// to tell the legal status from the minute column. The ruling restores §28's
// own "Status" heading, which the card dropped when the weekly table became a
// single day. Neither occurrence of "Counted" is renamed.
// ═══════════════════════════════════════════════════════════════════════════
describe("the status field carries its own label", () => {
  it("labels it 'Status' as a native term/definition pair", async () => {   // A
    render(<MyWorkingTime />);
    await settled();

    const dt = [...document.querySelectorAll("dt")]
      .find((el) => el.textContent === "Status");
    expect(dt).toBeTruthy();
    expect(dt.tagName).toBe("DT");

    const dd = dt.nextElementSibling;
    expect(dd.tagName).toBe("DD");
    expect(dd.textContent).toBe("Complete");
    expect(dt.closest("dl")).toBe(dd.closest("dl"));   // one pair, one list

    // Native semantics, not an aria patch bolted onto a paragraph.
    for (const el of [dt, dd]) {
      expect(el.getAttribute("aria-label")).toBeNull();
      expect(el.getAttribute("aria-labelledby")).toBeNull();
      expect(el.getAttribute("role")).toBeNull();
    }
  });

  it("exposes the label to assistive tech as term → definition", async () => { // A
    render(<MyWorkingTime />);
    await settled();
    expect(screen.getAllByRole("term").map((e) => e.textContent))
      .toContain("Status");
    expect(screen.getAllByRole("definition").map((e) => e.textContent))
      .toContain("Complete");
  });

  it("tells Status→Counted from Counted→1h 0m on a shortfall day", async () => { // B
    authFetch.mockResolvedValue(ok(payload({
      status: "Counted", evaluation_state: "SHORTFALL",
      required_minutes: 480, qualifying_minutes: 420,
      gross_shortfall_minutes: 60, exclusion_minutes: 0,
      attributable_minutes: 60,
    })));
    render(<MyWorkingTime />);
    await settled();

    // Both meanings are on screen, both keep §28's wording, and each is bound
    // to a different label.
    expect(pairs()).toContainEqual(["Status", "Counted"]);
    expect(pairs()).toContainEqual(["Counted", "1h 0m"]);

    // The regression the audit found: an occurrence of "Counted" that belongs
    // to no pair at all. Every one must now be inside a <dt> or a <dd>.
    const occurrences = ownTextIs("Counted");
    expect(occurrences).toHaveLength(2);
    for (const el of occurrences) expect(["DT", "DD"]).toContain(el.tagName);
    expect(occurrences.map((el) => el.tagName).sort()).toEqual(["DD", "DT"]);
  });

  it("tells Status→Excluded from Excluded→30m", async () => {              // C
    // "Excluded" carries the SAME structural collision as "Counted" — §28 uses
    // it for both a Status value and the `exclusion_minutes` column. The page
    // is a pure consumer and prints whatever `status` arrives, so this fixture
    // adds no evaluation_state mapping anywhere; it exercises the structure.
    authFetch.mockResolvedValue(ok(payload({
      status: "Excluded", exclusion_minutes: 30,
    })));
    render(<MyWorkingTime />);
    await settled();

    expect(pairs()).toContainEqual(["Status", "Excluded"]);
    expect(pairs()).toContainEqual(["Excluded", "30m"]);

    const occurrences = ownTextIs("Excluded");
    expect(occurrences).toHaveLength(2);
    expect(occurrences.map((el) => el.tagName).sort()).toEqual(["DD", "DT"]);
  });

  it("omits the label too when there is no status", async () => {
    // A labelled empty field would be worse than no field: it asserts that a
    // status exists and is blank.
    authFetch.mockResolvedValue(ok(payload({
      status: null, body: null, has_explanation: false, copy_keys: [],
      evaluation_state: "REQUIREMENT_UNRESOLVED",
    })));
    render(<MyWorkingTime />);
    await settled();
    expect(statusValue()).toBeNull();
    expect(pairs().map(([label]) => label)).not.toContain("Status");
  });

  it("leaves the five metric labels exactly as they were", async () => {   // E
    authFetch.mockResolvedValue(ok(payload({ status: "Counted" })));
    render(<MyWorkingTime />);
    await settled();
    const figures = [...document.querySelectorAll("dl")]
      .find((dl) => dl.querySelectorAll("dt").length === 5);
    expect([...figures.querySelectorAll("dt")].map((e) => e.textContent))
      .toEqual(["Required", "Qualifying", "Gross", "Excluded", "Counted"]);
  });

  it("leaves the transient live region untouched", async () => {           // F
    render(<MyWorkingTime />);
    await settled();
    const live = document.querySelector('[role="status"]');
    expect(live.getAttribute("aria-live")).toBe("polite");
    // Ready state: empty, and the §28.1 Status did NOT move into it.
    expect(live.textContent).toBe("");
    expect(live.querySelector("dl")).toBeNull();
    expect(live.textContent).not.toContain("Complete");
    // Still does its own job.
    expect(document.querySelectorAll('[role="status"]').length).toBe(1);
  });

  it("still carries transient messages in that same live region", async () => { // F
    authFetch.mockResolvedValue(fail(404));
    render(<MyWorkingTime />);
    await screen.findByText(/no working-time record is available/i);
    const live = document.querySelector('[role="status"]');
    expect(live.textContent).toMatch(/no working-time record is available/i);
    expect(statusValue()).toBeNull();          // and no day card at all
  });

  it("keeps the Deferred body and pairs it with a labelled status", async () => { // D
    authFetch.mockResolvedValue(ok(payload({
      status: "Deferred", body: DEFERRED_BODY, has_explanation: true,
      evaluation_state: "DEFERRED_PENDING_LEDGER",
    })));
    render(<MyWorkingTime />);
    await settled();
    expect(pairs()).toContainEqual(["Status", "Deferred"]);
    expect(screen.getByText(DEFERRED_BODY).textContent).toBe(DEFERRED_BODY);
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// Outcomes
// ═══════════════════════════════════════════════════════════════════════════
describe("outcomes", () => {
  it("treats 404 as one generic unavailable outcome", async () => {   // 13
    authFetch.mockResolvedValue(fail(404));
    render(<MyWorkingTime />);
    expect(await screen.findByText(/no working-time record is available/i))
      .toBeTruthy();
    // Exactly one request: it must not probe to find out WHY.
    expect(authFetch).toHaveBeenCalledTimes(1);
    // And it says nothing about rollout or record existence.
    const body = document.body.textContent.toLowerCase();
    for (const leak of ["rollout", "shadow", "disabled", "not enabled",
                        "off for"]) {
      expect(body).not.toContain(leak);
    }
  });

  it("handles 403 without exposing an authority reason", async () => { // 14
    authFetch.mockResolvedValue(fail(403));
    render(<MyWorkingTime />);
    expect(await screen.findByText(/do not have access/i)).toBeTruthy();
    const body = document.body.textContent;
    for (const leak of ["NO_GRANT", "CROSS_TENANT", "NO_TENANT_CONTEXT"]) {
      expect(body).not.toContain(leak);
    }
  });

  it("handles a 500 and a network failure", async () => {              // 15
    authFetch.mockResolvedValue(fail(500));
    const { unmount } = render(<MyWorkingTime />);
    expect(await screen.findByText(/couldn't load/i)).toBeTruthy();
    unmount();

    authFetch.mockRejectedValue(new Error("offline"));
    render(<MyWorkingTime />);
    expect(await screen.findByText(/couldn't load/i)).toBeTruthy();
  });

  it("shows no stale figures after a failed refresh", async () => {
    // findAll/queryAll: in the default payload required == qualifying == 480,
    // which is exactly what a complete day looks like, so "8h 0m" is on screen
    // twice. The assertion is that NONE survive the failed refresh.
    render(<MyWorkingTime />);
    expect((await screen.findAllByText("8h 0m")).length).toBeGreaterThan(0);
    authFetch.mockResolvedValue(fail(500));
    fireEvent.change(screen.getByLabelText(/date/i),
                     { target: { value: "2026-09-01" } });
    await waitFor(() => expect(screen.queryAllByText("8h 0m")).toHaveLength(0));
  });

  it("exposes a loading state semantically", async () => {             // 16
    let release;
    authFetch.mockReturnValue(new Promise((r) => { release = r; }));
    render(<MyWorkingTime />);
    const main = document.querySelector("main");
    expect(main.getAttribute("aria-busy")).toBe("true");
    expect(screen.getByRole("status")).toBeTruthy();
    release(ok(payload()));
    await waitFor(() => expect(main.getAttribute("aria-busy")).toBe("false"));
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// Accessibility
// ═══════════════════════════════════════════════════════════════════════════
describe("accessibility", () => {
  it("has one semantic h1", async () => {                              // 28
    render(<MyWorkingTime />);
    expect(document.querySelectorAll("h1").length).toBe(1);
    expect(await screen.findByRole("heading", { level: 1 })).toBeTruthy();
    await settled();
  });

  it("gives the date control an accessible name", async () => {        // 29
    render(<MyWorkingTime />);
    const input = screen.getByLabelText(/date/i);
    expect(input.tagName).toBe("INPUT");
    expect(input.type).toBe("date");      // keyboard-operable natively
    await settled();
  });

  it("renders status and body as readable text, not decoration", async () => { // 30
    authFetch.mockResolvedValue(ok(payload({
      status: "Deferred", body: DEFERRED_BODY, has_explanation: true,
    })));
    render(<MyWorkingTime />);
    const status = await screen.findByText("Deferred");
    const body = await screen.findByText(DEFERRED_BODY);
    // Real text nodes — not a title attribute, aria-label or background colour.
    expect(status.textContent).toBe("Deferred");
    expect(body.textContent).toBe(DEFERRED_BODY);
  });
});

// ═══════════════════════════════════════════════════════════════════════════
// Static guards — the page must not re-implement backend semantics
// ═══════════════════════════════════════════════════════════════════════════
describe("the page duplicates no backend semantics", () => {
  it("hardcodes no ECO explanatory copy", () => {                      // 17
    expect(PAGE).not.toContain(DEFERRED_BODY);
    for (const fragment of ["exit is still being resolved",
                            "shortfall is not final",
                            "contributes 0 minutes",
                            "neither counted nor excused"]) {
      expect(PAGE.toLowerCase()).not.toContain(fragment.toLowerCase());
      expect(CLIENT.toLowerCase()).not.toContain(fragment.toLowerCase());
    }
  });

  it("hardcodes no §28.1 status label", () => {                        // 18
    // As STRING LITERALS, which is what a mapping would need.
    //
    // "Counted" is exempt, and deliberately: §28's day table has a Counted
    // COLUMN as well as a Counted status, and the column header is the
    // specification's own wording for `attributable_minutes`. The guard that
    // actually prevents a status mapping is the next one — the page cannot map
    // what it never reads.
    for (const label of ["Deferred", "Not a working day", "Complete"]) {
      expect(PAGE_CODE).not.toContain(`"${label}"`);
      expect(PAGE_CODE).not.toContain(`'${label}'`);
    }
  });

  it("never reads evaluation_state to decide what to display", () => { // 19
    expect(PAGE_CODE).not.toContain("evaluation_state");
    for (const state of ["DEFERRED_PENDING_LEDGER", "NO_SHORTFALL",
                         "SHORTFALL", "NOT_A_WORKING_DAY",
                         "REQUIREMENT_UNRESOLVED", "EVALUABLE"]) {
      expect(PAGE_CODE).not.toContain(state);
    }
  });

  it("performs no ECO arithmetic over the minute fields", () => {      // 20, 24
    // Any expression combining two server-supplied minute values would be a
    // second calculation. None of the field names may appear beside an
    // operator, and nothing may reduce or aggregate them.
    const fields = ["required_minutes", "qualifying_minutes",
                    "gross_shortfall_minutes", "exclusion_minutes",
                    "attributable_minutes",
                    "contributes_to_accumulator_minutes"];
    for (const f of fields) {
      expect(PAGE_CODE).not.toMatch(new RegExp(`${f}\\s*[-+*/]`));
      expect(PAGE_CODE).not.toMatch(new RegExp(`[-+*/]\\s*\\w*\\.?${f}`));
    }
    for (const forbidden of [".reduce(", "weekly", "workweek", "sevenDay"]) {
      expect(PAGE_CODE).not.toContain(forbidden);
    }
  });

  it("reconstructs no seconds and no exclusion categories", () => {    // 25, 26
    for (const forbidden of ["qualifying_seconds", "duration_seconds",
                             "rounding", "employer closure",
                             "authorised release", "authorized release",
                             "none recorded", "none found"]) {
      expect(PAGE_CODE.toLowerCase()).not.toContain(forbidden.toLowerCase());
    }
  });

  it("uses the shared transport and no second HTTP client", () => {
    expect(CLIENT_CODE).toContain("authFetch");
    for (const own of ["new XMLHttpRequest", "axios.create"]) {
      expect(CLIENT_CODE).not.toContain(own);
    }
    // The client calls authFetch, never fetch directly.
    expect(CLIENT_CODE).not.toMatch(/(^|[^h])\bfetch\s*\(/m);
    expect(PAGE_CODE).not.toContain("fetch(");
    expect(PAGE_CODE).not.toContain("API_BASE");
  });

  it("implements no rollout logic of its own", () => {
    // Word-boundaried: a bare "OFF" substring matches ordinary identifiers.
    for (const forbidden of ["OFF", "SHADOW", "ENABLED", "rollout"]) {
      expect(PAGE_CODE).not.toMatch(new RegExp(`\\b${forbidden}\\b`));
    }
  });

  it("adds no navigation entry (OD-03 is unruled)", () => {
    const app = SRC("../App.jsx");
    expect(app).toContain('path="/my-working-time"');
    expect(app).toContain("MyWorkingTime");
    // A route only — no label, and no link anywhere in the app.
    for (const f of ["../components/TopNavbar.jsx", "../pages/Dashboard.jsx"]) {
      expect(SRC(f)).not.toContain("my-working-time");
    }
  });
});
