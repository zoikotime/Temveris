/**
 * Leave Balance card in the admin leave-review modal.
 *
 * Uses the numbers from the approved design: entitlement 15, accrued 12.5,
 * 4 decided days used, 1 day pending (the request under review), 7.5
 * available. The backend reports `used` INCLUDING pending (5) and `remaining`
 * already net of pending (7.5); the card must show Used = used - pending.
 */
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import LeaveBalanceCard from "../components/leave/LeaveBalanceCard";

const C = { border: "#ddd", surface: "#fff", text: "#111", sub: "#666" };
const DESIGN_ROW = { total: 15, accrued: 12.5, used: 5, pending: 1, remaining: 7.5 };

describe("LeaveBalanceCard", () => {
  it("shows the five numbers from the design", () => {
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} asOf={new Date(2026, 9, 7)} />);
    expect(screen.getByText("15.00")).toBeTruthy();   // Annual Entitlement
    expect(screen.getByText("12.50")).toBeTruthy();   // Accrued
    expect(screen.getByText("4.00")).toBeTruthy();    // Used = 5 - 1 pending
    expect(screen.getByText("1.00")).toBeTruthy();    // Pending
    expect(screen.getByText("7.50")).toBeTruthy();    // Available Balance
    expect(screen.getByText("Available Balance")).toBeTruthy();
  });

  it("labels the bar with used and available percentages of the entitlement", () => {
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} asOf={new Date(2026, 9, 7)} />);
    expect(screen.getByText("4.00 days used (27%)")).toBeTruthy();
    expect(screen.getByText("7.50 days available (50%)")).toBeTruthy();
  });

  it("keeps its own labels when the shared translation strings are lowercase fragments", () => {
    // enStrings has `used: "used"` and `days: "Days"` for other screens.
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} T={{ used: "used", days: "Days" }} />);
    expect(screen.getByText("Used")).toBeTruthy();
    expect(screen.queryByText("used")).toBeNull();
    expect(screen.getAllByText("days").length).toBe(5);
  });

  it("shows the as-of date", () => {
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} asOf={new Date(2026, 9, 7)} />);
    expect(screen.getByText(/As of 7 Oct 2026/)).toBeTruthy();
  });

  it("treats a missing pending field as zero (older backend)", () => {
    render(<LeaveBalanceCard C={C} balance={{ total: 10, accrued: 10, used: 3, remaining: 7 }} />);
    expect(screen.getByText("3.00")).toBeTruthy();
    expect(screen.getByText("0.00")).toBeTruthy();
  });

  it("shows a loading state", () => {
    render(<LeaveBalanceCard C={C} loading />);
    expect(screen.getByText("Loading balance…")).toBeTruthy();
  });

  it("degrades gracefully when the balance cannot be loaded", () => {
    render(<LeaveBalanceCard C={C} error />);
    expect(screen.getByText("Balance unavailable for this leave type.")).toBeTruthy();
  });

  it("shows taken/pending, not a fake balance, for Compensatory (no annual limit)", () => {
    render(<LeaveBalanceCard C={C} balance={{ no_annual_limit: true, is_earned: true,
      total: 0, accrued: 0, used: 3, pending: 1, remaining: 0 }} />);
    expect(screen.getByText("This leave type has no annual limit.")).toBeTruthy();
    expect(screen.getByText(/days used/).textContent).toMatch(/2\.00 days used · 1\.00 pending/);
    expect(screen.queryByText("Available Balance")).toBeNull();
  });

  it("hides Accrued when it only repeats the entitlement", () => {
    render(<LeaveBalanceCard C={C} balance={{ total: 15, accrued: 15, used: 3, pending: 3, remaining: 12 }} />);
    expect(screen.queryByText("Accrued")).toBeNull();
    expect(screen.getAllByText("15.00").length).toBe(1);
  });

  it("explains that pending includes the request under review", () => {
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} request={{ status: "submitted", days: 1 }} />);
    expect(screen.getByText(/Pending includes this request \(1\.00 days\)/)).toBeTruthy();
  });

  it("does not claim the request is pending once it is decided", () => {
    render(<LeaveBalanceCard C={C} balance={DESIGN_ROW} request={{ status: "approved", days: 1 }} />);
    expect(screen.queryByText(/Pending includes this request/)).toBeNull();
  });

  it("does not show numbers for an unlimited leave type", () => {
    render(<LeaveBalanceCard C={C} balance={{ is_unlimited: true, total: 999, used: 2, remaining: 997 }} />);
    expect(screen.getByText("This leave type has no annual limit.")).toBeTruthy();
    expect(screen.queryByText("999.00")).toBeNull();
  });
});

/**
 * Shared pool: Casual / Sick / Emergency / … draw from one balance sized by the
 * employee's Annual entitlement. The review of request #197 showed Casual's own
 * 15 / 0 used / 3 pending next to an Available of 5.30 that came from the pool,
 * so the numbers on screen could not add up.
 */
describe("LeaveBalanceCard — shared pool", () => {
  // The value shown under a stat label (the breakdown line repeats numbers).
  const stat = (label) => screen.getByText(label).nextSibling.textContent;
  const casual = (over = {}) => ({ total: 15, accrued: 15, used: 3, pending: 3, remaining: 5.3, shared_pool: true, ...over });
  const map = ({ poolTotal, poolUsed, poolRemaining, sickUsed = 0 }) => ({
    "Annual Leave": { total: poolTotal, accrued: poolTotal, used: 0, pending: 0, remaining: poolRemaining, shared_pool: true },
    "Casual Leave": casual(),
    "Sick Leave":   { total: 15, accrued: 15, used: sickUsed, pending: 0, remaining: poolRemaining, shared_pool: true },
    _shared_pool:   { enabled: true, total: poolTotal, used: poolUsed, remaining: poolRemaining,
                      source: "Annual Leave", members: ["Casual Leave", "Sick Leave"] },
  });

  it("uses the pool's numbers when the pool is smaller than the type's own cap (mid-year joiner)", () => {
    const balances = map({ poolTotal: 8.3, poolUsed: 3, poolRemaining: 5.3 });
    render(<LeaveBalanceCard C={C} balance={balances["Casual Leave"]} balances={balances} leaveType="Casual Leave" />);
    expect(stat("Shared Entitlement")).toBe("8.30");
    expect(stat("Used")).toBe("0.00");
    expect(stat("Pending")).toBe("3.00");
    expect(stat("Available Balance")).toBe("5.30");  // 8.30 - 0 - 3
    expect(screen.queryByText("15.00")).toBeNull();  // the type's own cap is not the headline
    expect(screen.getByTestId("leave-balance-pool-note").textContent)
      .toMatch(/Casual Leave shares one balance with Annual Leave, Sick Leave\..*Annual Leave entitlement/);
    expect(screen.getByTestId("leave-balance-pool-breakdown").textContent)
      .toMatch(/Casual Leave 3\.00 \(3\.00 pending\)/);
  });

  it("counts days taken under other pooled types as used", () => {
    const balances = map({ poolTotal: 15, poolUsed: 9.7, poolRemaining: 5.3, sickUsed: 6.7 });
    render(<LeaveBalanceCard C={C} balance={balances["Casual Leave"]} balances={balances} leaveType="Casual Leave" />);
    expect(stat("Shared Entitlement")).toBe("15.00");
    expect(stat("Used")).toBe("6.70");               // 9.70 taken from the pool - 3.00 pending
    expect(stat("Pending")).toBe("3.00");
    expect(stat("Available Balance")).toBe("5.30");  // 15.00 - 6.70 - 3.00
    expect(screen.getByTestId("leave-balance-pool-breakdown").textContent).toMatch(/Sick Leave 6\.70/);
  });

  it("explains when the type's own cap is tighter than the pool", () => {
    const balances = map({ poolTotal: 15, poolUsed: 6, poolRemaining: 9 });
    balances["Emergency Leave"] = { total: 6, accrued: 6, used: 6, pending: 0, remaining: 0, shared_pool: true };
    balances._shared_pool.members.push("Emergency Leave");
    balances["Casual Leave"] = casual({ used: 0, pending: 0, remaining: 9 });
    render(<LeaveBalanceCard C={C} balance={balances["Emergency Leave"]} balances={balances} leaveType="Emergency Leave" />);
    expect(screen.getByText(/Emergency Leave has its own limit of 6\.00 days a year/)).toBeTruthy();
  });

  it("falls back to the type's own numbers when the pool row is missing (older backend)", () => {
    render(<LeaveBalanceCard C={C} balance={casual({ remaining: 12 })} leaveType="Casual Leave" />);
    expect(screen.getByText("Annual Entitlement")).toBeTruthy();
    expect(screen.queryByTestId("leave-balance-pool-note")).toBeNull();
    expect(screen.getByText("12.00")).toBeTruthy();
  });
});
