/**
 * The navy sidebar restyle keeps every existing destination (no item from the
 * design mockup's shorter list replaces them), and the account menu that
 * replaced the always-visible Sign Out button still signs the admin out.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

vi.mock("../context/ThemeContext", () => ({ useTheme: () => ({ isDark: false }) }));
vi.mock("../context/LanguageContext", () => ({ useLanguage: () => ({ T: {} }) }));
const logout = vi.fn();
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { id: 1, name: "Ada Admin", email: "ada@x.test", role: "admin" }, logout }),
}));
vi.mock("../context/WorkspaceContext", () => ({
  useWorkspace: () => ({ workspaceName: "Temveris", workspaceLogo: null }),
}));
const navigate = vi.fn();
vi.mock("react-router-dom", () => ({
  useNavigate: () => navigate,
  useLocation: () => ({ pathname: "/admin", search: "?tab=overview" }),
}));
vi.mock("../context/EntitlementsContext", () => ({
  useEntitlements: () => ({ projection: null }),
}));

import AdminSidebar from "../components/AdminSidebar";

beforeEach(() => { navigate.mockClear(); logout.mockClear(); });

describe("AdminSidebar — navy restyle", () => {
  it("keeps every current section and item", () => {
    render(<AdminSidebar />);
    for (const label of [
      "Overview", "Team Directory", "Screenshots", "Attendance & Timesheets", "Time Playback",
      "Reporting", "Offboarding", "Activity Log",
      "Apps History", "Website Categories", "Leave Management", "Leave Summary", "Public Holidays",
      "Payroll Calculator", "Billing", "Workspace Settings",
    ]) {
      expect(screen.getByRole("button", { name: new RegExp(`^${label.replace(/[&]/g, "\\$&")}`) }), label).toBeInTheDocument();
    }
    expect(screen.queryByText(/Goals/)).not.toBeInTheDocument();
    // Sister-product links (Sema, HR, Payroll) were removed in the Temveris rename.
    expect(screen.queryByText(/sema/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/^communication$/i)).not.toBeInTheDocument();
  });

  it("marks the current page and shows the workspace under the logo", () => {
    render(<AdminSidebar />);
    expect(screen.getByRole("button", { name: "Overview" })).toHaveAttribute("aria-current", "page");
    expect(screen.getByRole("button", { name: "Team Directory" })).not.toHaveAttribute("aria-current");
    expect(screen.getByText("Temveris")).toBeInTheDocument();
  });

  it("signs out from the account menu", () => {
    render(<AdminSidebar />);
    expect(screen.queryByRole("menuitem", { name: /Sign Out/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /Sign Out/ }));
    expect(logout).toHaveBeenCalledWith({ redirect: false });
    expect(navigate).toHaveBeenCalledWith("/", { replace: true });
  });

  it("closes the account menu on a click outside it or Escape", () => {
    render(<AdminSidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    expect(screen.getByRole("menuitem", { name: /Sign Out/ })).toBeInTheDocument();
    fireEvent.mouseDown(document.body);
    expect(screen.queryByRole("menuitem", { name: /Sign Out/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("menuitem", { name: /Sign Out/ })).not.toBeInTheDocument();
  });

  it("opens My Profile from the account menu", () => {
    render(<AdminSidebar />);
    fireEvent.click(screen.getByRole("button", { name: "Account menu" }));
    fireEvent.click(screen.getByRole("menuitem", { name: /My Profile/ }));
    expect(navigate).toHaveBeenCalledWith("/profile");
  });
});
