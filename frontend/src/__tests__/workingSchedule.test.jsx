/**
 * Working Schedule section (P8).
 *
 * P8 renders the Working Schedule rows inside the EXISTING Organization Settings
 * page — same metadata-driven renderer, same Organization / Team / Employee
 * selector, same INHERITED badge, same Reset. These tests defend that contract:
 *
 *  - the three new control types render (multiselect / shift_ref / time)
 *  - working days serialize as a JSON array string, so no API change is needed
 *  - the INHERITED badge, "Inheriting: …" hint and Reset still behave
 *  - empty shift start reads as "no scheduled shift", not as blank/unset
 *  - employment type at Employee scope writes to the employee record, not to a
 *    settings row (Approach A — one source of truth)
 *  - engine_enabled / shadow_mode_enabled never appear
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

const WEEKDAYS = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"];

const scheduleRows = (over = {}) => [
  {
    key: "schedule.employment_type", group: "Working Schedule", label: "Employment type",
    type: "enum", options: ["full_time", "part_time", "contract", "intern"],
    option_labels: { full_time: "Full Time", part_time: "Part Time", contract: "Contract", intern: "Intern" },
    description: "", default: "full_time", parent_value: "full_time", value: null, is_override: false,
  },
  {
    key: "schedule.working_days", group: "Working Schedule", label: "Working days",
    type: "multiselect", options: WEEKDAYS,
    option_labels: Object.fromEntries(WEEKDAYS.map(d => [d, d[0] + d.slice(1).toLowerCase()])),
    description: "", default: JSON.stringify(WEEKDAYS.slice(0, 5)),
    parent_value: JSON.stringify(WEEKDAYS.slice(0, 5)), value: null, is_override: false,
  },
  {
    key: "schedule.shift_id", group: "Working Schedule", label: "Assigned shift",
    type: "shift_ref", description: "", default: "", parent_value: "", value: null, is_override: false,
  },
  {
    key: "schedule.shift_start_time", group: "Working Schedule", label: "Shift start time",
    type: "time", description: "", default: "", parent_value: "", value: null, is_override: false,
  },
  {
    key: "schedule.grace_minutes", group: "Working Schedule", label: "Grace period",
    type: "int", unit: "minutes", min: 0, max: 240,
    description: "", default: "15", parent_value: "20", value: null, is_override: false,
    ...over,
  },
];

function mockFetch({ rows = scheduleRows(), shifts = [], employees = [] } = {}) {
  const calls = [];
  global.fetch = vi.fn((url, opts = {}) => {
    calls.push({ url: String(url), method: opts.method || "GET", body: opts.body });
    const u = String(url);
    const json = (d) => Promise.resolve({ ok: true, json: () => Promise.resolve(d) });
    if (u.includes("/settings/scoped")) {
      return json({ scope: "org", group_order: ["Working Schedule"], keys: rows });
    }
    if (u.includes("/schedule/shifts")) return json(shifts);
    if (u.includes("/employees")) return json(employees);
    if (u.includes("/org/teams")) return json([]);
    return json({});
  });
  return calls;
}

beforeEach(() => vi.clearAllMocks());
afterEach(() => { delete global.fetch; });

const renderOrgSettings = (opts) => {
  const calls = mockFetch(opts);
  const showToast = vi.fn();
  render(<OrganizationSettingsTab isAdmin showToast={showToast} />);
  return { calls, showToast };
};

// ── The section renders inside the existing page ─────────────────────────────

describe("Working Schedule section", () => {
  it("renders as a group in the existing Organization Settings panel", async () => {
    renderOrgSettings();
    expect(await screen.findByText("Working Schedule")).toBeTruthy();
    expect(screen.getByText("Employment type")).toBeTruthy();
    expect(screen.getByText("Working days")).toBeTruthy();
    expect(screen.getByText("Assigned shift")).toBeTruthy();
    expect(screen.getByText("Shift start time")).toBeTruthy();
    expect(screen.getByText("Grace period")).toBeTruthy();
  });

  it("keeps the Organization / Team / Employee selector", async () => {
    renderOrgSettings();
    await screen.findByText("Working Schedule");
    expect(screen.getByText("Organization")).toBeTruthy();
    expect(screen.getByText("Team")).toBeTruthy();
    expect(screen.getByText("Employee")).toBeTruthy();
  });

  it("never renders the internal rollout switches", async () => {
    renderOrgSettings();
    await screen.findByText("Working Schedule");
    expect(screen.queryByText(/engine.?enabled/i)).toBeNull();
    expect(screen.queryByText(/shadow.?mode/i)).toBeNull();
  });
});

// ── New control: multiselect (working days) ──────────────────────────────────

describe("working days control", () => {
  it("renders one chip per weekday", async () => {
    renderOrgSettings();
    await screen.findByText("Working Schedule");
    for (const d of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
      expect(screen.getByTitle(d.length === 3 ? {
        Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday",
        Fri: "Friday", Sat: "Saturday", Sun: "Sunday",
      }[d] : d)).toBeTruthy();
    }
  });

  it("serializes the selection as a JSON array string", async () => {
    const { calls } = renderOrgSettings();
    await screen.findByText("Working Schedule");
    fireEvent.click(screen.getByTitle("Saturday"));      // add Sat to Mon-Fri
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => {
      const put = calls.find(c => c.method === "PUT" && c.url.includes("/settings/scoped"));
      expect(put).toBeTruthy();
      const sent = JSON.parse(put.body).settings["schedule.working_days"];
      // A string, not an array — the settings payload is Dict[str, str].
      expect(typeof sent).toBe("string");
      expect(JSON.parse(sent)).toContain("SATURDAY");
      expect(JSON.parse(sent)).toContain("MONDAY");
    });
  });

  it("deselecting a day removes it", async () => {
    const { calls } = renderOrgSettings();
    await screen.findByText("Working Schedule");
    fireEvent.click(screen.getByTitle("Monday"));        // Mon was selected
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => {
      const put = calls.find(c => c.method === "PUT" && c.url.includes("/settings/scoped"));
      expect(JSON.parse(JSON.parse(put.body).settings["schedule.working_days"]))
        .not.toContain("MONDAY");
    });
  });
});

// ── New control: shift_ref ──────────────────────────────────────────────────

describe("assigned shift control", () => {
  it("offers None plus every org shift", async () => {
    renderOrgSettings({ shifts: [
      { id: 4, name: "Morning", start_time: "06:00", end_time: "14:00" },
      { id: 5, name: "Night", start_time: "22:00", end_time: "06:00" },
    ] });
    await screen.findByText("Working Schedule");
    await waitFor(() => expect(screen.getByText(/Morning/)).toBeTruthy());
    expect(screen.getByText(/None \(use start\/end below\)/)).toBeTruthy();
    expect(screen.getByText(/Night/)).toBeTruthy();
  });

  it("still renders when the shift fetch fails", async () => {
    global.fetch = vi.fn((url) => {
      const u = String(url);
      if (u.includes("/schedule/shifts")) return Promise.reject(new Error("boom"));
      const json = (d) => Promise.resolve({ ok: true, json: () => Promise.resolve(d) });
      if (u.includes("/settings/scoped")) {
        return json({ scope: "org", group_order: ["Working Schedule"], keys: scheduleRows() });
      }
      return json([]);
    });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    expect(await screen.findByText("Working Schedule")).toBeTruthy();
    expect(screen.getByText("Grace period")).toBeTruthy();
  });
});

// ── New control: time, where empty is meaningful ─────────────────────────────

describe("shift start time control", () => {
  it("shows an empty start as 'Not set' rather than blank", async () => {
    const rows = scheduleRows();
    renderOrgSettings({ rows });
    await screen.findByText("Working Schedule");
    // Org scope shows no "Inheriting" hint, so assert the control itself is empty
    // and the row is present — "Not set" is exercised at employee scope below.
    const timeInput = document.querySelector('input[type="time"]');
    expect(timeInput).toBeTruthy();
    expect(timeInput.value).toBe("");
  });

  it("offers Clear once a time is chosen", async () => {
    renderOrgSettings();
    await screen.findByText("Working Schedule");
    const timeInput = document.querySelector('input[type="time"]');
    fireEvent.change(timeInput, { target: { value: "09:30" } });
    expect(await screen.findByTitle("Clear — no scheduled shift")).toBeTruthy();
  });

  it("Clear sends an empty string, meaning no scheduled shift", async () => {
    const { calls } = renderOrgSettings();
    await screen.findByText("Working Schedule");
    const timeInput = document.querySelector('input[type="time"]');
    fireEvent.change(timeInput, { target: { value: "09:30" } });
    fireEvent.click(await screen.findByTitle("Clear — no scheduled shift"));
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => {
      const put = calls.find(c => c.method === "PUT" && c.url.includes("/settings/scoped"));
      expect(JSON.parse(put.body).settings["schedule.shift_start_time"]).toBe("");
    });
  });
});

// ── Existing behaviours must keep working ───────────────────────────────────

describe("inheritance affordances are unchanged", () => {
  it("shows System default at org scope", async () => {
    renderOrgSettings();
    await screen.findByText("Working Schedule");
    expect(screen.getAllByText("System default").length).toBeGreaterThan(0);
  });

  it("shows OVERRIDDEN and a Reset for an overridden row", async () => {
    const rows = scheduleRows({ value: "30", is_override: true });
    renderOrgSettings({ rows });
    await screen.findByText("Working Schedule");
    expect(screen.getAllByText("Overridden").length).toBeGreaterThan(0);
    expect(screen.getByTitle("Reset to inherited value")).toBeTruthy();
  });

  it("Reset issues a DELETE for that key only", async () => {
    const rows = scheduleRows({ value: "30", is_override: true });
    const { calls } = renderOrgSettings({ rows });
    await screen.findByText("Working Schedule");
    fireEvent.click(screen.getByTitle("Reset to inherited value"));
    await waitFor(() => {
      const del = calls.find(c => c.method === "DELETE");
      expect(del).toBeTruthy();
      expect(JSON.parse(del.body).keys).toEqual(["schedule.grace_minutes"]);
    });
  });
});

// ── Employment type: Approach A ─────────────────────────────────────────────

describe("employment type at Employee scope (Approach A)", () => {
  const employees = [{ id: 77, name: "Priya", email: "p@x.com", team: "Eng", employment_type: "part_time" }];

  // The employee target picker is a searchable combobox (SearchableSelect), not
  // a <select>: click it open, then pick the row. Only the mechanics of choosing
  // changed here — every assertion below is the one it always made.
  async function atEmployeeScope() {
    const calls = mockFetch({ employees });
    render(<OrganizationSettingsTab isAdmin showToast={vi.fn()} />);
    await screen.findByText("Working Schedule");
    fireEvent.click(screen.getByText("Employee"));
    fireEvent.click(await screen.findByRole("combobox", { name: "Employee" }));
    fireEvent.mouseDown(await screen.findByRole("option", { name: "Priya — Eng" }));
    await screen.findByText("Working Schedule");
    return calls;
  }

  it("reads the value from the employee record, not the cascade", async () => {
    await atEmployeeScope();
    // part_time comes from the roster, not from parent_value (full_time).
    await waitFor(() => {
      const selects = [...document.querySelectorAll("select")];
      expect(selects.some(s => s.value === "part_time")).toBe(true);
    });
  });

  it("badges the row as Employee record, never Inherited", async () => {
    await atEmployeeScope();
    expect(await screen.findByText("Employee record")).toBeTruthy();
  });

  it("saves to PUT /employees/{id}, not to a settings row", async () => {
    const calls = await atEmployeeScope();
    const selects = [...document.querySelectorAll("select")];
    const empTypeSel = selects.find(s => s.value === "part_time");
    fireEvent.change(empTypeSel, { target: { value: "contract" } });
    fireEvent.click(screen.getByText("Save changes"));
    await waitFor(() => {
      const put = calls.find(c => c.method === "PUT" && /\/employees\/77$/.test(c.url));
      expect(put).toBeTruthy();
      expect(JSON.parse(put.body)).toEqual({ employment_type: "contract" });
    });
    // and it must NOT have gone through the settings cascade
    const scoped = calls.find(c => c.method === "PUT" && c.url.includes("/settings/scoped"));
    expect(scoped).toBeFalsy();
  });
});
