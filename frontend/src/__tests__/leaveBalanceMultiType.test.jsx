/**
 * Leave Balance Management — one adjustment applied to several leave types.
 *
 * HR sets an intern's paid types to 0 in one save instead of one save per type.
 * Each type is still its own POST /leave/admin/balances/adjust (unchanged
 * backend contract); a failure on one type is reported by name and does not
 * stop the others.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, configure } from "@testing-library/react";

// This page renders a large table plus the form; under a busy runner (full
// suite / CI) its async steps can exceed the 1s default and flake. Only
// failures ever wait this long.
configure({ asyncUtilTimeout: 5000 });
vi.setConfig({ testTimeout: 20000 });

const api = vi.hoisted(() => ({ wsGet: vi.fn(), wsSend: vi.fn() }));
vi.mock("../components/workspace/_api", () => api);
vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({
    C: { brand: "#1a73e8", border: "#ddd", surface: "#fff", text: "#111", textSub: "#666", bg: "#fafafa" },
  }),
}));

import LeaveBalanceManagementTab from "../components/workspace/LeaveBalanceManagementTab";

const TYPES = ["Annual Leave", "Casual Leave", "Sick Leave", "Unpaid Leave"];
const BAL = (allocated) => ({ allocated, used: 0, remaining: allocated });

beforeEach(() => {
  vi.clearAllMocks();
  api.wsGet.mockImplementation(async (path) => {
    if (path === "/leave/types") return TYPES.map(leave_type => ({ leave_type }));
    if (path.startsWith("/leave/admin/balances?")) {
      return { entitlement: 15, rows: [{ user_id: 7, name: "Vasu", email: "vasu@x.test",
        allocated: 15, used: 0, remaining: 15, status: "healthy" }] };
    }
    if (path === "/leave/admin/balances/7") {
      return { balances: { "Annual Leave": BAL(8.8), "Casual Leave": BAL(15),
                           "Sick Leave": BAL(15), "Unpaid Leave": BAL(0) } };
    }
    return {};
  });
  api.wsSend.mockImplementation(async (_m, _p, body) => ({
    leave_type: body.leave_type, new_allocation: body.adjustment_value, remaining: 0,
  }));
});

async function setup(showToast = vi.fn()) {
  render(<LeaveBalanceManagementTab isAdmin showToast={showToast} />);
  await screen.findByRole("option", { name: "Vasu" });
  fireEvent.change(screen.getByDisplayValue("Select an employee…"), { target: { value: "7" } });
  await waitFor(() => expect(api.wsGet).toHaveBeenCalledWith("/leave/admin/balances/7"));
  return showToast;
}

const openPicker = () => fireEvent.click(screen.getByRole("button", { name: /Annual Leave|leave types/i }));
const tick = (name) => fireEvent.click(screen.getByRole("checkbox", { name }));

describe("multi-type leave balance adjustment", () => {
  it("applies one Custom 0 adjustment to every selected type", async () => {
    const toast = await setup();
    openPicker();
    tick("Casual Leave");
    tick("Sick Leave");                       // Annual is selected by default
    fireEvent.click(screen.getByText("Custom Balance"));
    fireEvent.change(screen.getByPlaceholderText("0"), { target: { value: "0" } });

    const preview = await screen.findByTestId("multi-type-preview");
    expect(preview.textContent).toContain("Annual Leave");
    expect(preview.textContent).toContain("Sick Leave");

    fireEvent.click(screen.getByText("Save Adjustment"));
    fireEvent.click(screen.getByText("Confirm"));

    await waitFor(() => expect(api.wsSend).toHaveBeenCalledTimes(3));
    const sent = api.wsSend.mock.calls.map(c => [c[2].leave_type, c[2].adjustment_type, c[2].adjustment_value]);
    expect(sent).toEqual([
      ["Annual Leave", "custom", 0],
      ["Casual Leave", "custom", 0],
      ["Sick Leave", "custom", 0],
    ]);
    await waitFor(() => expect(toast).toHaveBeenCalledWith("Saved 3 of 3 leave types", "success"));
  });

  it("names a type that failed and still saves the rest", async () => {
    api.wsSend.mockImplementation(async (_m, _p, body) => {
      if (body.leave_type === "Casual Leave") throw new Error("boom");
      return { leave_type: body.leave_type, new_allocation: 0, remaining: 0 };
    });
    const toast = await setup();
    openPicker();
    tick("Casual Leave");
    fireEvent.click(screen.getByText("Custom Balance"));
    fireEvent.change(screen.getByPlaceholderText("0"), { target: { value: "0" } });
    fireEvent.click(screen.getByText("Save Adjustment"));
    fireEvent.click(screen.getByText("Confirm"));

    await waitFor(() => expect(api.wsSend).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      "Saved 1 of 2 leave types · failed: Casual Leave (boom)", "error"));
  });

  it("Select all / Clear drive the selection, and Save needs at least one type", async () => {
    await setup();
    openPicker();
    fireEvent.click(screen.getByText("Select all"));
    TYPES.forEach(t => expect(screen.getByRole("checkbox", { name: t }).checked).toBe(true));
    fireEvent.click(screen.getByText("Clear"));
    TYPES.forEach(t => expect(screen.getByRole("checkbox", { name: t }).checked).toBe(false));
    expect(screen.getByText("Save Adjustment").disabled).toBe(true);
  });

  it("with one type it keeps the single-type preview", async () => {
    await setup();
    fireEvent.click(screen.getByText("Custom Balance"));
    fireEvent.change(screen.getByPlaceholderText("0"), { target: { value: "0" } });
    expect(await screen.findByText(/Final allocation:/)).toBeTruthy();
    expect(screen.queryByTestId("multi-type-preview")).toBeNull();
  });
});
