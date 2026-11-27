/**
 * Setup-wizard tests.
 *
 * The wizard is where a self-registered admin configures the tenant that signup
 * just provisioned, so the properties that matter are: it renders the step the
 * SERVER says it is on (never client-remembered state), it reuses the existing
 * organization/team APIs rather than any new one, it never sends an organization
 * id, and a tenant that has finished is bounced out instead of being asked to
 * set up again.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});
vi.mock("../api/authStorage", () => ({ readToken: () => "test-token" }));

import OrganizationSetup from "../pages/OrganizationSetup";

const setupState = (over = {}) => ({
  current_step: "organization",
  setup_required: true,
  completed: false,
  completed_at: null,
  organization: {
    id: 7, company_name: "Engine House", default_timezone: "Asia/Kolkata",
    primary_country: "IN", logo_url: null,
  },
  admin: { id: 1, name: "Ada", email: "ada@engines.example.com", role: "admin" },
  teams: { count: 0, limit: null },
  seats: { used: 1, limit: 25 },
  ...over,
});

/** Route fetch by URL so tests assert on real call shapes. */
function mockFetch(handlers) {
  return vi.fn((url, opts = {}) => {
    for (const [fragment, handler] of handlers) {
      if (String(url).includes(fragment)) return Promise.resolve(handler(url, opts));
    }
    // The completion step renders DesktopDownloadCards, which probes for
    // mac/linux builds. Answered here by default (not-ok → the cards show
    // "Coming soon") so every test does not have to stub it. A test that cares
    // about download availability can still handle "/desktop/" explicitly,
    // since declared handlers are matched first.
    if (String(url).includes("/desktop/")) {
      return Promise.resolve(fail(503));
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
}
const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const fail = (status, body = {}) => ({ ok: false, status, json: async () => body });

const renderWizard = () =>
  render(<MemoryRouter><OrganizationSetup /></MemoryRouter>);

beforeEach(() => { mockNavigate.mockReset(); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("OrganizationSetup — server-driven step", () => {
  it("renders step 1 when the server says organization", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => ok(setupState())]]));
    renderWizard();
    expect(await screen.findByText("Create your organization")).toBeInTheDocument();
    expect(screen.getByText("Step 1 of 3")).toBeInTheDocument();
  });

  it("renders step 2 when the server says teams", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(setupState({ current_step: "teams" }))],
    ]));
    renderWizard();
    expect(await screen.findByText("Let's set up your teams")).toBeInTheDocument();
    expect(screen.getByText("Step 2 of 3")).toBeInTheDocument();
  });

  it("renders step 3 when the server says complete", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(setupState({ current_step: "complete" }))],
    ]));
    renderWizard();
    expect(await screen.findByText("Congratulations!")).toBeInTheDocument();
  });

  it("redirects a finished tenant away instead of asking it to set up again", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(setupState({
        setup_required: false, completed: true, current_step: "complete",
      }))],
    ]));
    renderWizard();
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/admin", { replace: true }));
  });

  it("sends an unauthenticated caller to login", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => fail(401)]]));
    renderWizard();
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith("/", { replace: true }));
  });

  it("offers a retry when the state cannot be loaded", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => fail(500)]]));
    renderWizard();
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /try again/i })).toBeInTheDocument();
  });
});

describe("OrganizationSetup — step 1 reuses the org settings API", () => {
  it("PUTs /org/settings and never sends an organization id", async () => {
    const calls = [];
    vi.stubGlobal("fetch", mockFetch([
      ["/org/settings", (u, o) => { calls.push(JSON.parse(o.body)); return ok({}); }],
      ["/onboarding/setup/step", () => ok(setupState({ current_step: "teams" }))],
      ["/onboarding/setup", () => ok(setupState())],
    ]));
    renderWizard();

    const input = await screen.findByPlaceholderText("Acme Corporation");
    fireEvent.change(input, { target: { value: "Engine House Ltd" } });
    fireEvent.click(screen.getByRole("button", { name: /save & continue/i }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].company_name).toBe("Engine House Ltd");
    // Tenancy is derived server-side from the authenticated user.
    expect(calls[0]).not.toHaveProperty("organization_id");
    expect(calls[0]).not.toHaveProperty("id");
  });

  it("requires a name before it will submit", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => ok(setupState())]]));
    renderWizard();
    const input = await screen.findByPlaceholderText("Acme Corporation");
    const submit = screen.getByRole("button", { name: /save & continue/i });

    // Prefilled from the server, so enabled to begin with.
    expect(input).toHaveValue("Engine House");
    expect(submit).toBeEnabled();

    fireEvent.change(input, { target: { value: "" } });
    expect(submit).toBeDisabled();

    // Whitespace is not a name either.
    fireEvent.change(input, { target: { value: "   " } });
    expect(submit).toBeDisabled();

    fireEvent.change(input, { target: { value: "Real Ltd" } });
    expect(submit).toBeEnabled();
  });

  it("surfaces a permission failure without advancing", async () => {
    const stepCalls = [];
    vi.stubGlobal("fetch", mockFetch([
      ["/org/settings", () => fail(403)],
      ["/onboarding/setup/step", () => { stepCalls.push(1); return ok(setupState()); }],
      ["/onboarding/setup", () => ok(setupState())],
    ]));
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText("Acme Corporation"),
      { target: { value: "X Ltd" } });
    fireEvent.click(screen.getByRole("button", { name: /save & continue/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/permission/i);
    expect(stepCalls).toHaveLength(0);
  });

  it("prefills the timezone the server already knows", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => ok(setupState())]]));
    renderWizard();
    await screen.findByPlaceholderText("Acme Corporation");
    expect(screen.getByPlaceholderText("Asia/Kolkata")).toHaveValue("Asia/Kolkata");
  });
});

describe("OrganizationSetup — step 2 reuses the teams API", () => {
  const teamsState = (over = {}) => setupState({ current_step: "teams", ...over });

  it("POSTs each team to /org/teams with no organization id", async () => {
    const created = [];
    vi.stubGlobal("fetch", mockFetch([
      ["/org/teams", (u, o) => { created.push(JSON.parse(o.body)); return ok({}); }],
      ["/onboarding/setup/step", () => ok(teamsState())],
      ["/onboarding/setup", () => ok(teamsState())],
    ]));
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText("Engineering"),
      { target: { value: "Engineering" } });
    fireEvent.click(screen.getByRole("button", { name: /add another team/i }));
    fireEvent.change(screen.getByPlaceholderText("Add another team"),
      { target: { value: "Operations" } });
    fireEvent.click(screen.getByRole("button", { name: /create & continue/i }));

    await waitFor(() => expect(created).toHaveLength(2));
    expect(created.map((c) => c.name)).toEqual(["Engineering", "Operations"]);
    created.forEach((c) => expect(c).not.toHaveProperty("organization_id"));
  });

  it("treats a duplicate team as already done rather than an error", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/org/teams", () => fail(409, { detail: "Team already exists" })],
      ["/onboarding/setup/step", () => ok(teamsState())],
      ["/onboarding/setup", () => ok(teamsState())],
    ]));
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText("Engineering"),
      { target: { value: "Engineering" } });
    fireEvent.click(screen.getByRole("button", { name: /create & continue/i }));

    await waitFor(() => expect(mockNavigate).not.toHaveBeenCalledWith("/", { replace: true }));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows the plan's team limit only when the server reports one", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(teamsState({ teams: { count: 0, limit: null } }))],
    ]));
    const { unmount } = renderWizard();
    await screen.findByPlaceholderText("Engineering");
    // No cap sold today, so nothing decorative is displayed.
    expect(screen.queryByText(/your plan includes/i)).toBeNull();
    unmount();

    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(teamsState({ teams: { count: 0, limit: 2 } }))],
    ]));
    renderWizard();
    expect(await screen.findByText(/your plan includes 2 teams/i)).toBeInTheDocument();
  });

  it("stops offering more teams once the server-stated cap is reached", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup", () => ok(teamsState({ teams: { count: 2, limit: 2 } }))],
    ]));
    renderWizard();
    await screen.findByPlaceholderText("Engineering");
    expect(screen.getByRole("button", { name: /add another team/i })).toBeDisabled();
  });

  it("surfaces a server-side limit rejection", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/org/teams", () => fail(403, {
        detail: { code: "TEAM_LIMIT_REACHED", message: "Your plan includes 2 teams." },
      })],
      ["/onboarding/setup", () => ok(teamsState())],
    ]));
    renderWizard();

    fireEvent.change(await screen.findByPlaceholderText("Engineering"),
      { target: { value: "Third" } });
    fireEvent.click(screen.getByRole("button", { name: /create & continue/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/2 teams/i);
  });

  it("can be skipped without creating any team", async () => {
    const created = [];
    vi.stubGlobal("fetch", mockFetch([
      ["/org/teams", (u, o) => { created.push(o); return ok({}); }],
      ["/onboarding/setup/step", () => ok(teamsState())],
      ["/onboarding/setup", () => ok(teamsState())],
    ]));
    renderWizard();
    fireEvent.click(await screen.findByRole("button", { name: /skip for now/i }));
    await waitFor(() => expect(created).toHaveLength(0));
  });
});

describe("OrganizationSetup — step 3", () => {
  const doneState = () => setupState({
    current_step: "complete", teams: { count: 2, limit: null },
  });

  it("summarises the organization, admin and team count", async () => {
    vi.stubGlobal("fetch", mockFetch([["/onboarding/setup", () => ok(doneState())]]));
    renderWizard();
    expect(await screen.findByText("Congratulations!")).toBeInTheDocument();
    expect(screen.getByText("Engine House")).toBeInTheDocument();
    expect(screen.getByText("ada@engines.example.com")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("completes then navigates to the chosen destination", async () => {
    let completed = 0;
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup/complete", () => { completed++; return ok(doneState()); }],
      ["/onboarding/setup", () => ok(doneState())],
    ]));
    renderWizard();
    fireEvent.click(await screen.findByRole("button", { name: /go to dashboard/i }));
    await waitFor(() => expect(completed).toBe(1));
    expect(mockNavigate).toHaveBeenCalledWith("/admin", { replace: true });
  });

  it("routes to the existing invite and settings screens", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup/complete", () => ok(doneState())],
      ["/onboarding/setup", () => ok(doneState())],
    ]));
    renderWizard();
    fireEvent.click(await screen.findByRole("button", { name: /invite team members/i }));
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/settings/workers-users", { replace: true }));
  });

  it("does not navigate when completion fails", async () => {
    vi.stubGlobal("fetch", mockFetch([
      ["/onboarding/setup/complete", () => fail(500)],
      ["/onboarding/setup", () => ok(doneState())],
    ]));
    renderWizard();
    fireEvent.click(await screen.findByRole("button", { name: /go to dashboard/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't finish/i);
    expect(mockNavigate).not.toHaveBeenCalledWith("/admin", { replace: true });
  });
});
