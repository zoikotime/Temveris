/**
 * Phase 3.7 — AdminSidebar consumes the projection: HIDE removes, LOCK/READ_ONLY
 * decorate, SHOW renders normally. Contexts are mocked so the test isolates the
 * sidebar's presentation behaviour.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// A permissive theme proxy so any C.* access returns a value.
const C = new Proxy({}, { get: () => "#123456" });
vi.mock("../context/ThemeContext", () => ({ useTheme: () => ({ C }) }));
vi.mock("../context/LanguageContext", () => ({ useLanguage: () => ({ T: {} }) }));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { id: 1, name: "Ada", email: "a@x.test", role: "admin" }, logout: vi.fn() }),
}));
vi.mock("../context/WorkspaceContext", () => ({
  useWorkspace: () => ({ workspaceName: "WS", workspaceLogo: null }),
}));
vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
  useLocation: () => ({ pathname: "/admin", search: "" }),
}));

// The controllable projection for each test.
let projectionValue = null;
vi.mock("../context/EntitlementsContext", () => ({
  useEntitlements: () => ({ projection: projectionValue }),
  PRESENTATION: {
    SHOW: "show", LOCK: "lock", HIDE: "hide",
    READ_ONLY: "read_only", CONFIGURATION_REQUIRED: "configuration_required",
  },
}));

import AdminSidebar from "../components/AdminSidebar";

beforeEach(() => { projectionValue = null; });

const full = (over = {}) => ({
  "dashboard.overview.view": "show",
  "team.directory.view": "show",
  "evidence.screenshots.view": "show",
  "time.attendance.view": "show",
  "time.playback.basic": "show",
  "reports.standard": "show",
  "offboarding.standard": "show",
  "activity.log.view": "show",
  "productivity.apps_history": "hide",
  "productivity.website_categories": "hide",
  "leave.manage": "show",
  "holidays.primary": "show",
  "payroll.calculator": "show",
  "billing.portal": "show",
  "workspace.settings.core": "show",
  ...over,
});

describe("AdminSidebar — projection presentation", () => {
  it("renders all nav items when the projection has not loaded (fallback)", () => {
    projectionValue = null;
    render(<AdminSidebar />);
    expect(screen.getByText("Reporting")).toBeInTheDocument();
    // Privacy items render in the fallback (no server enforcement), because the
    // frontend never invents visibility — only an explicit HIDE removes them.
    expect(screen.getByText("Apps History")).toBeInTheDocument();
  });

  it("HIDE removes privacy items", () => {
    projectionValue = full();
    render(<AdminSidebar />);
    expect(screen.queryByText("Apps History")).not.toBeInTheDocument();
    expect(screen.queryByText("Website Categories")).not.toBeInTheDocument();
    // ...while SHOW items remain.
    expect(screen.getByText("Reporting")).toBeInTheDocument();
    expect(screen.getByText("Payroll Calculator")).toBeInTheDocument();
  });

  it("LOCK keeps the item visible with a lock badge", () => {
    projectionValue = full({ "payroll.calculator": "lock" });
    render(<AdminSidebar />);
    const label = screen.getByText("Payroll Calculator");
    expect(label).toBeInTheDocument();
    // the 🔒 badge is a sibling in the same button
    expect(label.closest("button").textContent).toContain("🔒");
  });

  it("READ_ONLY keeps the item visible with an RO badge", () => {
    projectionValue = full({ "reports.standard": "read_only" });
    render(<AdminSidebar />);
    const label = screen.getByText("Reporting");
    expect(label.closest("button").textContent).toContain("RO");
  });

  it("CONFIGURATION_REQUIRED shows a setup badge, never a lock", () => {
    projectionValue = full({ "reports.standard": "configuration_required" });
    render(<AdminSidebar />);
    const btn = screen.getByText("Reporting").closest("button");
    expect(btn.textContent).toContain("SET UP");
    expect(btn.textContent).not.toContain("🔒");
  });

  it("SHOW renders the item with no presentation badge", () => {
    projectionValue = full();
    render(<AdminSidebar />);
    const btn = screen.getByText("Reporting").closest("button");
    expect(btn.textContent).not.toContain("🔒");
    expect(btn.textContent).not.toContain("RO");
    expect(btn.textContent).not.toContain("SET UP");
  });
});
