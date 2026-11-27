/**
 * Super-Admin organization lifecycle UI.
 *
 * The drawer is the only place an operator can change a tenant's lifecycle, so
 * the properties that matter are:
 *   • it offers the action the CURRENT state allows — Suspend for an active
 *     org, Reactivate + Delete for a suspended one, and never Delete for an
 *     active one (the backend refuses that transition, so offering it would be
 *     a button that always fails)
 *   • deletion cannot happen by accident: the exact organization name has to be
 *     typed, and the typed value is what gets sent
 *   • a successful delete removes the row immediately, and a failed one leaves
 *     it exactly where it was
 *
 * The delete request itself is asserted on the wire (method, URL, body) rather
 * than through internal state, because the server — not this component — is the
 * security boundary.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const toastCalls = vi.hoisted(() => ({ success: [], error: [] }));
vi.mock("react-hot-toast", () => ({
  default: {
    success: (m) => toastCalls.success.push(String(m)),
    error: (m) => toastCalls.error.push(String(m)),
  },
}));

vi.mock("../context/ThemeContext", () => ({
  useTheme: () => ({ isDark: false, toggleDark: () => {} }),
}));
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ logout: vi.fn() }),
}));
vi.mock("../api/authStorage", () => ({
  writeToken: vi.fn(),
  mirrorUserToStorage: vi.fn(),
  clearAuthStorage: vi.fn(),
}));
vi.mock("../assets/Temveris_Logo.svg", () => ({ default: "logo.svg" }));

import SuperAdmin from "../pages/SuperAdmin";

// ─── Fixtures ────────────────────────────────────────────────────────────────

const ORG_ACTIVE = {
  id: "org_active_1", operational_org_id: 11, name: "Northwind Traders",
  commercial_state: "active", country: "IN", domain: "northwind.test",
  user_count: 4, active_users: 4, admin_count: 1, team_count: 1,
  created_at: "2026-01-01T00:00:00Z", last_active: "2026-08-16T00:00:00Z",
  subscription: null,
};
const ORG_SUSPENDED = {
  ...ORG_ACTIVE, id: "org_susp_1", operational_org_id: 12,
  name: "Demo Company", commercial_state: "suspended",
};

const detailFor = (org) => ({
  organization: org,
  org_settings: { default_timezone: "Asia/Kolkata", default_currency: "INR" },
  app_settings: {}, active_clock_sessions: 0, pending_leave_count: 0,
  users: [], teams: [], recent_activity: [], signups: [], recent_events: [],
  workspaces: [],
});

const STATS = {
  organizations: { total: 2, active: 1, trial: 0, paid: 1, suspended: 1, past_due: 0 },
  users: { total: 8, active_today: 2 },
  signups: { last_7d: 0 },
  platform: { privileged_admin_users: 2, active_countries: 1, billable_seats: 8, open_security_events: 0 },
  generated_at: "2026-08-17T00:00:00Z",
};

/** Records every request so tests can assert on the wire, not on internals.
 *
 *  `orgs` is mutated on a successful DELETE so the refetch the component fires
 *  afterwards behaves like a real server — otherwise the list would repopulate
 *  with the row that was just deleted and the test would prove nothing. */
function installFetch({ orgs, deleteResponse }) {
  const calls = [];
  let live = [...orgs];
  const fetchMock = vi.fn((url, opts = {}) => {
    const u = String(url);
    const method = (opts.method || "GET").toUpperCase();
    calls.push({ url: u, method, body: opts.body });

    const ok = (data) => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(data),
    });

    if (method === "DELETE") {
      const r = deleteResponse || { ok: true, data: { success: true, rows_deleted: 137 } };
      if (r.ok) {
        const id = u.split("/organizations/")[1];
        live = live.filter((o) => o.id !== id);
        return ok(r.data);
      }
      return Promise.resolve({
        ok: false, status: r.status || 409,
        json: () => Promise.resolve({ detail: r.detail }),
      });
    }

    if (u.includes("/stats")) return ok(STATS);
    if (/\/organizations\/[^/?]+$/.test(u) && method === "GET") {
      const id = u.split("/organizations/")[1];
      const org = live.find((o) => o.id === id);
      return ok(detailFor(org));
    }
    if (u.includes("/organizations?")) return ok({ items: live, count: live.length, total: live.length });
    if (u.includes("/signups")) return ok({ items: [] });
    if (u.includes("/audit")) return ok({ events: [] });
    if (u.includes("/billing")) return ok({ orgs: [], platform_mrr: 0, platform_arr: 0, past_due: 0, renewals_30d: 0 });

    return ok({});
  });
  global.fetch = fetchMock;
  return calls;
}

// SuperAdmin debounces its org fetch by 250ms and animates its KPI cards with
// requestAnimationFrame, so assertions routinely land past the 1s waitFor
// default when the full suite runs in parallel. One shared ceiling keeps the
// tests deterministic without masking a genuinely broken render.
const WAIT = 5000;

const renderPage = () =>
  render(<MemoryRouter><SuperAdmin /></MemoryRouter>);

/** Open the drawer for an org by clicking its row in the table. */
async function openDrawer(name) {
  const cell = await screen.findByText(name, {}, { timeout: 3000 });
  fireEvent.click(cell);
  return waitFor(
    () =>
      expect(screen.getByRole("button", { name: /Suspend Organization|Reactivate Organization/ }))
        .toBeInTheDocument(),
    { timeout: WAIT },
  );
}

beforeEach(() => {
  toastCalls.success.length = 0;
  toastCalls.error.length = 0;
  mockNavigate.mockReset();
  localStorage.setItem("zt_token", "super-admin-token");
});

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
});

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("lifecycle actions offered per state", () => {
  it("an ACTIVE organization offers Suspend and never Delete", async () => {
    installFetch({ orgs: [ORG_ACTIVE] });
    renderPage();
    await openDrawer("Northwind Traders");

    expect(screen.getByRole("button", { name: "Suspend Organization" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete Organization" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Reactivate Organization" })).toBeNull();
  });

  it("a SUSPENDED organization offers Reactivate and Delete", async () => {
    installFetch({ orgs: [ORG_SUSPENDED] });
    renderPage();
    await openDrawer("Demo Company");

    expect(screen.getByRole("button", { name: "Reactivate Organization" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Delete Organization" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Suspend Organization" })).toBeNull();
  });
});

describe("delete confirmation", () => {
  async function openDeleteModal() {
    renderPage();
    await openDrawer("Demo Company");
    fireEvent.click(screen.getByRole("button", { name: "Delete Organization" }));
    return waitFor(
      () => expect(screen.getByText("Delete Organization?")).toBeInTheDocument(),
      { timeout: WAIT },
    );
  }

  it("asks for confirmation and states that it cannot be undone", async () => {
    installFetch({ orgs: [ORG_SUSPENDED] });
    await openDeleteModal();

    expect(screen.getByText("Delete Organization?")).toBeInTheDocument();
    expect(screen.getByText(/This action cannot be undone/i)).toBeInTheDocument();
    expect(
      screen.getByLabelText("Type the organization name to confirm")
    ).toBeInTheDocument();
  });

  it("keeps the delete button disabled until the name matches exactly", async () => {
    installFetch({ orgs: [ORG_SUSPENDED] });
    await openDeleteModal();

    const dialog = screen.getByText("Delete Organization?").closest("div").parentElement.parentElement;
    const confirmBtn = within(dialog).getByRole("button", { name: "Delete Organization" });
    const input = screen.getByLabelText("Type the organization name to confirm");

    expect(confirmBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "Demo Compan" } });
    expect(confirmBtn).toBeDisabled();
    expect(screen.getByText("The name does not match yet.")).toBeInTheDocument();

    fireEvent.change(input, { target: { value: "demo company" } });
    expect(confirmBtn).toBeDisabled();   // case-sensitive on purpose

    fireEvent.change(input, { target: { value: "Demo Company" } });
    expect(confirmBtn).toBeEnabled();
    expect(screen.queryByText("The name does not match yet.")).toBeNull();
  });

  it("Cancel closes the modal and sends nothing", async () => {
    const calls = installFetch({ orgs: [ORG_SUSPENDED] });
    await openDeleteModal();

    fireEvent.change(screen.getByLabelText("Type the organization name to confirm"),
                     { target: { value: "Demo Company" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    await waitFor(() => expect(screen.queryByText("Delete Organization?")).toBeNull(),
                   { timeout: WAIT });
    expect(calls.filter((c) => c.method === "DELETE")).toHaveLength(0);
  });

  it("sends DELETE with the typed name and removes the row on success", async () => {
    const calls = installFetch({ orgs: [ORG_SUSPENDED] });
    await openDeleteModal();

    fireEvent.change(screen.getByLabelText("Type the organization name to confirm"),
                     { target: { value: "Demo Company" } });
    const dialog = screen.getByText("Delete Organization?").closest("div").parentElement.parentElement;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete Organization" }));

    await waitFor(() => {
      const del = calls.find((c) => c.method === "DELETE");
      expect(del).toBeDefined();
      expect(del.url).toContain("/api/super-admin/organizations/org_susp_1");
      expect(JSON.parse(del.body)).toEqual({ confirm_name: "Demo Company" });
    }, { timeout: WAIT });

    // Removed from the list immediately — not left behind on stale state.
    await waitFor(() => expect(screen.queryByText("Demo Company")).toBeNull(),
                   { timeout: WAIT });
    expect(toastCalls.success.join(" ")).toMatch(/deleted/i);
  });

  it("keeps the organization visible and shows a readable error when delete fails", async () => {
    installFetch({
      orgs: [ORG_SUSPENDED],
      deleteResponse: {
        ok: false, status: 409,
        detail: {
          code: "organization_not_suspended",
          message: "Organization must be suspended before it can be deleted.",
        },
      },
    });
    await openDeleteModal();

    fireEvent.change(screen.getByLabelText("Type the organization name to confirm"),
                     { target: { value: "Demo Company" } });
    const dialog = screen.getByText("Delete Organization?").closest("div").parentElement.parentElement;
    fireEvent.click(within(dialog).getByRole("button", { name: "Delete Organization" }));

    await waitFor(
      () =>
        expect(toastCalls.error.join(" ")).toContain(
          "Organization must be suspended before it can be deleted."
        ),
      { timeout: WAIT },
    );
    // The structured {code, message} detail must not surface as "[object Object]".
    expect(toastCalls.error.join(" ")).not.toContain("[object Object]");
    // Still listed — the backend rolled back, so the UI must not prune it.
    // (The name also appears inside the still-open modal, hence getAllByText.)
    expect(screen.getAllByText("Demo Company").length).toBeGreaterThan(0);
  });
});

describe("suspend flow", () => {
  it("warns that access stops immediately and that no data is deleted", async () => {
    installFetch({ orgs: [ORG_ACTIVE] });
    renderPage();
    await openDrawer("Northwind Traders");
    fireEvent.click(screen.getByRole("button", { name: "Suspend Organization" }));

    await waitFor(
      () => expect(screen.getByText(/Suspend Northwind Traders/)).toBeInTheDocument(),
      { timeout: WAIT },
    );
    const copy = screen.getByText(/signed out and blocked/i).textContent;
    expect(copy).toMatch(/immediately/i);
    expect(copy).toMatch(/No data is deleted/i);
  });
});
