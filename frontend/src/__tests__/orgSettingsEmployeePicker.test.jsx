/**
 * Organization Settings → Employee — the employee picker.
 *
 * Two things are defended here.
 *
 * **Who may be offered.** The picker used to read the plain `GET /employees`,
 * which deliberately includes deactivated rows (Workers & Users renders an
 * Inactive badge; the offboarding directory resolves former workers) — so every
 * removed account showed up as a selectable settings target. It now asks for
 * `?active_only=true` and re-reads the roster when the scope is reopened.
 *
 * **How it is browsed.** A real roster runs to dozens of names, so the control
 * is a searchable, scrollable combobox rather than a native `<select>` that can
 * only be jumped through by first letter.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

import OrganizationSettingsTab from "../components/OrganizationSettingsTab";

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({
    C: {
      brand: "#1a73e8", brandBg: "#e8f0fe", border: "#ddd", borderLight: "#eee",
      surface: "#fff", text: "#111", textSub: "#666", textLight: "#999", bg: "#fafafa",
    },
  }),
}));

const emp = (id, name, over = {}) => ({
  id, name, email: `${name.toLowerCase().replace(/ /g, ".")}@example.com`,
  role: "employee", team: null, department: null, employment_type: "full_time",
  is_active: true, is_invited: false, is_password_set: true,
  onboarding_status: "active", ...over,
});

/**
 * `roster` may be an array (constant) or a function called per request, which is
 * how the "removed in another tab" cases change the answer mid-session.
 */
function mockFetch({ roster = [] } = {}) {
  const calls = [];
  global.fetch = vi.fn((url, opts = {}) => {
    const u = String(url);
    calls.push({ url: u, method: opts.method || "GET" });
    const json = (d) => Promise.resolve({ ok: true, json: () => Promise.resolve(d) });
    if (u.includes("/settings/scoped")) return json({ scope: "org", group_order: [], keys: [] });
    if (u.includes("/schedule/shifts")) return json([]);
    if (u.includes("/org/teams")) return json([]);
    if (u.includes("/employees")) {
      return json(typeof roster === "function" ? roster() : roster);
    }
    return json({});
  });
  return calls;
}

const employeeUrls = (calls) => calls.filter(c => c.url.includes("/employees")).map(c => c.url);

// The scope tab. Local state only — no remount, which is why the roster refetch
// hangs off it.
const gotoEmployeeScope = () => fireEvent.click(screen.getByText("Employee"));

/** Open the closed combobox so its options are rendered. */
const openPicker = async () =>
  fireEvent.click(await screen.findByRole("combobox", { name: "Employee" }));

const gotoAndOpen = async () => { gotoEmployeeScope(); await openPicker(); };

const searchBox = () => screen.getByRole("combobox", { name: /Search employees/ });
const optionNames = () => screen.getAllByRole("option").map(o => o.textContent.replace("✓", ""));

beforeEach(() => vi.clearAllMocks());
afterEach(() => { delete global.fetch; });

// ── Who may be offered ───────────────────────────────────────────────────────

describe("Organization Settings employee picker — eligibility", () => {
  it("asks the backend for selectable employees only", async () => {
    const calls = mockFetch({ roster: [emp(1, "Active One")] });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);

    await waitFor(() => expect(employeeUrls(calls).length).toBeGreaterThan(0));
    employeeUrls(calls).forEach(u => expect(u).toContain("active_only=true"));
  });

  it("lists valid employees of the organization", async () => {
    mockFetch({ roster: [emp(1, "Alice", { team: "QA" }), emp(2, "Bob")] });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    expect(await screen.findByRole("option", { name: "Alice — QA" })).toBeTruthy();
    expect(screen.getByRole("option", { name: "Bob" })).toBeTruthy();
  });

  it("does not offer a deactivated employee even if the API leaks one", async () => {
    // Deploy-skew guard: a backend that ignores active_only, or a cached
    // response, must not repopulate the dropdown with revoked accounts.
    mockFetch({
      roster: [emp(1, "Alice"), emp(2, "Removed", { is_active: false, onboarding_status: "inactive" })],
    });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    expect(await screen.findByRole("option", { name: "Alice" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Removed" })).toBeNull();
  });

  it("does not offer a super admin as an employee", async () => {
    mockFetch({ roster: [emp(1, "Alice"), emp(9, "Platform Support", { role: "super_admin" })] });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    expect(await screen.findByRole("option", { name: "Alice" })).toBeTruthy();
    expect(screen.queryByRole("option", { name: "Platform Support" })).toBeNull();
  });

  it("drops an employee removed elsewhere when the Employee scope is reopened", async () => {
    let removed = false;
    mockFetch({ roster: () => (removed ? [emp(1, "Alice")] : [emp(1, "Alice"), emp(2, "Doomed")]) });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);

    await gotoAndOpen();
    expect(await screen.findByRole("option", { name: "Doomed" })).toBeTruthy();

    // Deleted from Workers & Users in another tab, then the admin comes back
    // here via Organization → Employee without reloading the page.
    removed = true;
    fireEvent.click(screen.getByText("Organization"));
    await gotoAndOpen();

    await waitFor(() => expect(screen.queryByRole("option", { name: "Doomed" })).toBeNull());
    expect(screen.getByRole("option", { name: "Alice" })).toBeTruthy();
  });

  it("clears a selection whose employee is no longer selectable", async () => {
    let removed = false;
    mockFetch({ roster: () => (removed ? [emp(1, "Alice")] : [emp(1, "Alice"), emp(2, "Doomed")]) });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);

    await gotoAndOpen();
    fireEvent.mouseDown(await screen.findByRole("option", { name: "Doomed" }));
    expect(await screen.findByRole("combobox", { name: "Employee" })).toHaveTextContent("Doomed");

    removed = true;
    fireEvent.click(screen.getByText("Organization"));
    gotoEmployeeScope();

    // Falls back to the placeholder rather than holding a dangling target that
    // a save would still write settings for.
    await waitFor(() => expect(
      screen.getByRole("combobox", { name: "Employee" })).toHaveTextContent("Select an employee…"));
  });

  it("renders no options for an empty organization", async () => {
    mockFetch({ roster: [] });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    expect(await screen.findByText("No employees match")).toBeTruthy();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });
});

// ── How it is browsed ────────────────────────────────────────────────────────

describe("Organization Settings employee picker — search and scroll", () => {
  const roster = [
    emp(1, "Alice", { team: "QA" }),
    emp(2, "Bob", { team: "Web Development" }),
    emp(3, "Charan", { team: "AI Development" }),
    emp(4, "Priya Sharma", { team: "Web Development" }),
  ];

  it("shows the whole roster on open, before anything is typed", async () => {
    // The failure mode of a <datalist>: options only appear once you type, so an
    // admin who wants to browse sees an empty box.
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(4));
  });

  it("filters the list as you type a name", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.change(searchBox(), { target: { value: "cha" } });
    await waitFor(() => expect(optionNames()).toEqual(["Charan — AI Development"]));
  });

  it("filters on team as well as name", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.change(searchBox(), { target: { value: "web development" } });
    await waitFor(() => expect(optionNames().sort())
      .toEqual(["Bob — Web Development", "Priya Sharma — Web Development"]));
  });

  it("filters on email, which is searchable but not shown on every row", async () => {
    // Two people with the same first name are told apart by typing the address,
    // without putting 60 addresses on screen.
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.change(searchBox(), { target: { value: "priya.sharma@" } });
    await waitFor(() => expect(optionNames()).toEqual(["Priya Sharma — Web Development"]));
  });

  it("reports how much the filter narrowed the roster", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    expect(await screen.findByText("4 of 4")).toBeTruthy();
    fireEvent.change(searchBox(), { target: { value: "web development" } });
    expect(await screen.findByText("2 of 4")).toBeTruthy();
  });

  it("says so when nothing matches, rather than showing an empty panel", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.change(searchBox(), { target: { value: "zzzz" } });
    expect(await screen.findByText("No employees match")).toBeTruthy();
  });

  it("scrolls the option list instead of growing the page", async () => {
    mockFetch({ roster: Array.from({ length: 60 }, (_, i) => emp(i + 1, `Worker ${i + 1}`)) });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    const listbox = await screen.findByRole("listbox", { name: "Employee" });
    expect(listbox.style.overflowY).toBe("auto");
    expect(parseInt(listbox.style.maxHeight, 10)).toBeGreaterThan(0);
    // All 60 stay reachable — the cap is on height, not on options.
    expect(screen.getAllByRole("option")).toHaveLength(60);
  });

  it("is keyboard navigable and commits with Enter", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    const box = searchBox();
    fireEvent.change(box, { target: { value: "web" } });   // Bob, then Priya Sharma
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(2));
    fireEvent.keyDown(box, { key: "ArrowDown" });           // past Bob, onto Priya
    fireEvent.keyDown(box, { key: "Enter" });

    await waitFor(() => expect(
      screen.getByRole("combobox", { name: "Employee" })).toHaveTextContent("Priya Sharma"));
  });

  it("closes on Escape without changing the selection", async () => {
    mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.keyDown(searchBox(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(screen.getByRole("combobox", { name: "Employee" })).toHaveTextContent("Select an employee…");
  });

  it("selecting an employee loads that employee's scoped settings", async () => {
    const calls = mockFetch({ roster });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await gotoAndOpen();

    fireEvent.mouseDown(await screen.findByRole("option", { name: "Charan — AI Development" }));
    await waitFor(() => expect(calls.some(c =>
      c.url.includes("/settings/scoped") && c.url.includes("scope=employee") && c.url.includes("user_id=3"),
    )).toBe(true));
  });
});
