/**
 * Steps 3 and 4 of the commercial activation flow — Workspace and Ready.
 *
 * WHAT THESE GUARD
 * ----------------
 *   1. **Nothing is invented.** Every milestone, the reference id, the trial
 *      length and the workspace name are rendered from the status response.
 *      A page that could show progress without the backend saying so would be
 *      a lie told to someone waiting on a tenant.
 *   2. **Ready is a backend fact.** /workspace/ready before provisioning
 *      completes bounces back to preparing; reaching `ready` moves forward.
 *   3. **Retry is backend-controlled.** The page asks the idempotent endpoint
 *      once, and only when the server says a retry is available.
 *   4. **No payment.** Neither Ready action creates a checkout session.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

// Classic JSX runtime under vitest — see the note in authShell.test.jsx.
globalThis.React = React;

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

const mockAuthFetch = vi.fn();
vi.mock("../api/authFetch", () => ({
  authFetch: (...a) => mockAuthFetch(...a),
  authFetchJson: vi.fn(),
  default: (...a) => mockAuthFetch(...a),
}));

import WorkspaceActivation from "../pages/WorkspaceActivation";

// ── Fixtures ─────────────────────────────────────────────────────────────────
const MILESTONES = [
  { key: "account_verified", title: "Account verified", description: "Your email is confirmed." },
  { key: "organisation_created", title: "Organisation created", description: "Workspace record set up." },
  { key: "workspace_provisioning", title: "Workspace provisioning", description: "Building your secure workspace." },
  { key: "trial_entitlements", title: "Trial entitlements applying", description: "Enabling your plan features." },
  { key: "security_audit", title: "Security and audit controls", description: "Applying policy and audit setup." },
  { key: "ready", title: "Ready", description: "Your workspace is ready." },
];

/** Build a milestone list with the first `completeCount` marked complete. */
function milestones(completeCount, { failed = false } = {}) {
  let markedFirstPending = false;
  return MILESTONES.map((m, i) => {
    let status = "pending";
    if (i < completeCount) status = "complete";
    else if (!markedFirstPending) {
      status = failed ? "failed" : "active";
      markedFirstPending = true;
    }
    return { ...m, status };
  });
}

const BASE = {
  signup_id: "sgn_1",
  organization_id: "org_1",
  workspace_id: "wsp_1",
  correlation_id: "cor_7f9a3b2c1122",
  trace_id: "trc_x",
  support_reference: "ZT-7F9A-3B2C",
  entitlements_ready: true,
  email_verified: true,
  organization_name: "Acme Corporation",
  owner_first_name: "sadik",
  workspace_slug: "asd",
  workspace_name: "Acme Corporation",
  trial: {
    length_days: 14,
    started_at: "2026-08-24T09:00:00+00:00",
    ends_at: "2026-09-07T09:00:00+00:00",
    plan_code: "verified",
    state: "trial_active",
  },
};

const IN_PROGRESS = {
  ...BASE,
  status: "in_progress",
  workspace_ready: false,
  workspace_url: null,
  milestones: milestones(3),
  provisioning: {
    state: "owner_assigned", current_step: "Assigning owner",
    completed_steps: [], total_steps: 9, percent_complete: 50,
    failed_step: null, failure_code: null, retry_available: false,
    estimated_seconds_remaining: 90,
  },
};

const READY = {
  ...BASE,
  status: "ready",
  workspace_ready: true,
  workspace_url: "/dashboard",
  milestones: milestones(6),
  provisioning: {
    state: "ready", current_step: "Workspace ready", completed_steps: [],
    total_steps: 9, percent_complete: 100, failed_step: null,
    failure_code: null, retry_available: false, estimated_seconds_remaining: null,
  },
};

const FAILED = {
  ...BASE,
  status: "failed",
  workspace_ready: false,
  workspace_url: null,
  milestones: milestones(2, { failed: true }),
  provisioning: {
    state: "failed_retryable", current_step: null, completed_steps: [],
    total_steps: 9, percent_complete: 30, failed_step: "Creating workspace",
    failure_code: "PROVISIONING_FAILED", retry_available: true,
    estimated_seconds_remaining: null,
  },
};

const ok = (body, status = 200) => ({ ok: true, status, json: async () => body });
const fail = (body, status) => ({ ok: false, status, json: async () => body });

/**
 * @param status  a body, or a function of the poll count (for progressions)
 * @param retry   the /activation/retry response
 */
function mockApi({ status = READY, retry } = {}) {
  const calls = { status: [], retry: 0 };
  mockAuthFetch.mockImplementation(async (url, init) => {
    const u = String(url);
    if (u.includes("/activation/retry")) {
      calls.retry += 1;
      return retry || ok({ retried: true, provisioning_state: "ready" });
    }
    if (u.includes("/activation/status")) {
      calls.status.push(u);
      const body = typeof status === "function" ? status(calls.status.length) : status;
      if (body === 401) return fail({}, 401);
      if (body === 404) return fail({}, 404);
      return ok(body);
    }
    throw new Error(`unexpected authFetch: ${u} ${init ? init.method : ""}`);
  });
  return calls;
}

function renderAt(url = "/workspace/preparing") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/workspace/preparing" element={<WorkspaceActivation />} />
        <Route path="/workspace/ready" element={<WorkspaceActivation />} />
      </Routes>
    </MemoryRouter>,
  );
}

let errorSpy;
beforeEach(() => {
  mockNavigate.mockClear();
  mockAuthFetch.mockReset();
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  errorSpy.mockRestore();
  vi.restoreAllMocks();
});


// ═════════════════════════════════════════════════════════════════════════════
// Step 3 — Workspace
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 3 — preparing", () => {
  it("marks Account and Verify complete and Workspace current", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    await screen.findByText(/preparing your workspace/i);

    const progress = screen.getByRole("navigation", { name: /activation progress/i });
    expect(within(progress).getByText(/Step 1 of 4: completed/i)).toBeInTheDocument();
    expect(within(progress).getByText(/Step 2 of 4: completed/i)).toBeInTheDocument();
    expect(within(progress).getByText(/Step 3 of 4: current step/i)).toBeInTheDocument();
    expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent("Workspace");
  });

  it("shows the heading and supporting copy", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    expect(await screen.findByRole("heading", { name: /preparing your workspace/i })).toBeInTheDocument();
    expect(screen.getByText(/this usually takes a minute or two/i)).toBeInTheDocument();
  });

  it("renders the six milestones the server sent", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    const list = await screen.findByRole("list", { name: /provisioning progress/i });
    for (const m of MILESTONES) {
      expect(within(list).getByText(m.title)).toBeInTheDocument();
      expect(within(list).getByText(m.description)).toBeInTheDocument();
    }
  });

  it("reflects the server's per-milestone state, not a local guess", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    const list = await screen.findByRole("list", { name: /provisioning progress/i });
    const items = within(list).getAllByRole("listitem");

    // Three complete, the fourth in progress, the rest not started — exactly
    // what the fixture said. State is in text, not only in colour.
    expect(items[0]).toHaveTextContent(/Complete/);
    expect(items[2]).toHaveTextContent(/Complete/);
    expect(items[3]).toHaveTextContent(/In progress/);
    expect(items[5]).toHaveTextContent(/Not started/);
  });

  it("advances milestones as the backend advances, with no timer", async () => {
    // Two polls, two different server states. The page changes because the
    // server changed — nothing here is driven by elapsed time.
    mockApi({
      status: (n) => (n === 1
        ? { ...IN_PROGRESS, milestones: milestones(2) }
        : { ...IN_PROGRESS, milestones: milestones(5) }),
    });
    renderAt();

    const list = await screen.findByRole("list", { name: /provisioning progress/i });
    await waitFor(() =>
      expect(within(list).getAllByRole("listitem")[2]).toHaveTextContent(/In progress/));
    await waitFor(() =>
      expect(within(list).getAllByRole("listitem")[4]).toHaveTextContent(/Complete/),
      { timeout: 6000 });
  });

  it("shows the estimated time from the server", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    expect(await screen.findByText(/estimated time/i)).toBeInTheDocument();
    expect(screen.getByText(/2 minutes/i)).toBeInTheDocument();
  });

  it("renders the support footer with the server's reference id", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt();
    expect(await screen.findByText(/experiencing issues\?/i)).toBeInTheDocument();
    // The reference is derived from the signup's correlation_id server-side and
    // is stable across refreshes — never generated in the browser.
    expect(screen.getByText("ZT-7F9A-3B2C")).toBeInTheDocument();
    expect(screen.getByText(/need help\?/i)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /contact support/i }))
      .toHaveAttribute("href", "mailto:support@temveris.example");
  });

  it("polls the session-scoped endpoint when no identifier is in the URL", async () => {
    // Step 2 hands over with no query string; the tenant comes from the
    // session, which is what makes isolation structural.
    const calls = mockApi({ status: IN_PROGRESS });
    renderAt();
    await waitFor(() => expect(calls.status.length).toBeGreaterThan(0));
    expect(calls.status[0]).toContain("/activation/status/me");
  });

  it("still honours an identifier when one is supplied", async () => {
    const calls = mockApi({ status: IN_PROGRESS });
    renderAt("/workspace/preparing?signup_id=sgn_1");
    await waitFor(() => expect(calls.status.length).toBeGreaterThan(0));
    expect(calls.status[0]).toContain("signup_id=sgn_1");
    expect(calls.status[0]).not.toContain("/status/me");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Failure and retry
// ═════════════════════════════════════════════════════════════════════════════
describe("Activation chrome", () => {
  it("brands every step with the Temveris mark", async () => {
    // The flow is the first thing a customer sees. It opened with a bare step
    // indicator and nothing identifying the product.
    for (const [scene, path] of [[IN_PROGRESS, "/workspace/preparing"],
                                 [READY, "/workspace/ready"]]) {
      mockApi({ status: scene });
      const { unmount } = renderAt(path);
      await screen.findByRole("heading", { level: 1 });
      expect(screen.getByAltText("Temveris")).toBeInTheDocument();
      unmount();
    }
  });

  it("does not make the mark a way out of a half-finished signup", async () => {
    mockApi({ status: IN_PROGRESS });
    renderAt("/workspace/preparing");
    await screen.findByRole("heading", { level: 1 });
    // A logo that navigates away mid-flow is a trapdoor: the tenant exists
    // server-side but the customer has left the only screen tracking it.
    expect(screen.getByAltText("Temveris").closest("a")).toBeNull();
  });
});


describe("Step 3 — failure and retry", () => {
  it("asks the backend to retry when the server says a retry is available", async () => {
    const calls = mockApi({ status: FAILED });
    renderAt();
    await waitFor(() => expect(calls.retry).toBe(1));
    expect(await screen.findByText(/retry in progress/i)).toBeInTheDocument();
    expect(screen.getByText(/retrying provisioning automatically/i)).toBeInTheDocument();
  });

  it("retries only once, however many times the page polls", async () => {
    // The endpoint is idempotent, but a retry fired on every poll would both
    // hammer it and make "retrying" look permanent.
    const calls = mockApi({ status: FAILED });
    renderAt();
    await waitFor(() => expect(calls.retry).toBe(1));
    await new Promise((r) => setTimeout(r, 400));
    expect(calls.retry).toBe(1);
  });

  it("does not retry when the backend says the failure is not retryable", async () => {
    const terminal = {
      ...FAILED,
      provisioning: { ...FAILED.provisioning, state: "failed_terminal", retry_available: false },
    };
    const calls = mockApi({ status: terminal });
    renderAt();
    await screen.findByText(/experiencing issues\?/i);
    await new Promise((r) => setTimeout(r, 300));
    expect(calls.retry).toBe(0);
    expect(screen.queryByText(/retrying provisioning automatically/i)).not.toBeInTheDocument();
  });

  it("does not retry a tenant addressed by identifier rather than by session", async () => {
    // A support-shared URL is someone looking at a tenant, not the owner
    // waiting on one — it must not mutate anything.
    const calls = mockApi({ status: FAILED });
    renderAt("/workspace/preparing?organization_id=org_1");
    await screen.findByText(/experiencing issues\?/i);
    await new Promise((r) => setTimeout(r, 300));
    expect(calls.retry).toBe(0);
  });

  it("marks the failed milestone rather than claiming it finished", async () => {
    mockApi({ status: FAILED });
    renderAt();
    const list = await screen.findByRole("list", { name: /provisioning progress/i });
    expect(within(list).getAllByRole("listitem")[2]).toHaveTextContent(/Needs attention/);
  });

  it("never exposes internals in the failure copy", async () => {
    mockApi({ status: FAILED });
    renderAt();
    await screen.findByText(/experiencing issues\?/i);
    expect(document.body.textContent).not.toMatch(/traceback|sqlalchemy|psycopg|File "/i);
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Route reconciliation
// ═════════════════════════════════════════════════════════════════════════════
describe("Ready is a backend fact", () => {
  it("moves to Ready when the server reports ready", async () => {
    mockApi({ status: READY });
    renderAt("/workspace/preparing");
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/workspace/ready", { replace: true }));
  });

  it("bounces back to preparing when Ready is opened too early", async () => {
    // Direct navigation, an old tab, a bookmark — none of them may show a
    // workspace that is not finished.
    mockApi({ status: IN_PROGRESS });
    renderAt("/workspace/ready");
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/workspace/preparing", { replace: true }));
    // Asserted on the heading, not the text: "Your workspace is ready." is also
    // the description of the final milestone in the preparing timeline.
    expect(screen.queryByRole("heading", { name: /your workspace is ready/i }))
      .not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: /preparing your workspace/i })).toBeInTheDocument();
  });

  it("does not show Ready content while provisioning has failed", async () => {
    mockApi({ status: FAILED });
    renderAt("/workspace/ready");
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/workspace/preparing", { replace: true }));
    expect(screen.queryByRole("heading", { name: /your workspace is ready/i }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /claim .* free trial/i })).not.toBeInTheDocument();
  });

  it("re-reads state from the server after a refresh", async () => {
    // Nothing about progress lives in the browser, so a remount simply asks
    // again and resumes wherever the tenant actually is.
    const calls = mockApi({ status: IN_PROGRESS });
    const { unmount } = renderAt();
    await waitFor(() => expect(calls.status.length).toBeGreaterThan(0));
    unmount();

    const after = mockApi({ status: READY });
    renderAt();
    await waitFor(() => expect(after.status.length).toBeGreaterThan(0));
    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/workspace/ready", { replace: true }));
  });

  it("prompts sign-in rather than guessing when the session is gone", async () => {
    mockApi({ status: 401 });
    renderAt();
    expect(await screen.findByRole("heading", { name: /sign in to continue/i })).toBeInTheDocument();
  });

  it("offers a way back when no activation matches", async () => {
    mockApi({ status: 404 });
    renderAt();
    expect(await screen.findByRole("heading", { name: /couldn't find that workspace/i })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to sign up/i })).toHaveAttribute("href", "/start");
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Step 4 — Ready
// ═════════════════════════════════════════════════════════════════════════════
describe("Step 4 — ready", () => {
  async function reachReady(body = READY) {
    mockApi({ status: body });
    renderAt("/workspace/ready");
    await screen.findByRole("heading", { name: /your workspace is ready/i });
  }

  it("marks the first three steps complete", async () => {
    await reachReady();
    const progress = screen.getByRole("navigation", { name: /activation progress/i });
    for (const n of [1, 2, 3]) {
      expect(within(progress).getByText(new RegExp(`Step ${n} of 4: completed`, "i"))).toBeInTheDocument();
    }
    expect(progress.querySelector('[aria-current="step"]')).toHaveTextContent("Ready");
  });

  it("greets the user and names the workspace from the server", async () => {
    await reachReady();
    expect(screen.getByText(/welcome aboard, sadik/i)).toBeInTheDocument();
    // The slug the admin chose on Step 2, with the environment's display
    // suffix. A label, not a link — the platform does not route by host.
    expect(screen.getByText("asd.temveris.example")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /temveris\.example/i })).not.toBeInTheDocument();
  });

  it("takes the trial length from the server, not from a constant", async () => {
    await reachReady();
    expect(screen.getByText(/is live for 14 days/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /claim 14 day free trial/i })).toBeInTheDocument();
  });

  it("follows the server when the trial length differs", async () => {
    // The proof that 14 is read, not hardcoded.
    await reachReady({ ...READY, trial: { ...READY.trial, length_days: 21 } });
    expect(screen.getByText(/is live for 21 days/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /claim 21 day free trial/i })).toBeInTheDocument();
    expect(screen.queryByText(/30 days/i)).not.toBeInTheDocument();
  });

  it("names the plan from the server", async () => {
    await reachReady();
    expect(screen.getByText(/Temveris Verified trial workspace/i)).toBeInTheDocument();
  });

  it("opens the workspace without creating a checkout or a subscription", async () => {
    const calls = mockApi({ status: READY });
    renderAt("/workspace/ready");
    await screen.findByRole("heading", { name: /your workspace is ready/i });

    fireEvent.click(screen.getByRole("button", { name: /claim 14 day free trial/i }));
    expect(mockNavigate).toHaveBeenCalledWith("/dashboard");

    // The trial was provisioned at signup. Claiming it again would be a second
    // subscription; nothing here posts anywhere.
    const mutations = mockAuthFetch.mock.calls.filter(
      ([, init]) => init && init.method && init.method !== "GET",
    );
    expect(mutations).toHaveLength(0);
    expect(calls.retry).toBe(0);
  });

  it("sends Configure your subscription to the checkout configurator", async () => {
    await reachReady();
    // The configurator sizes and buys a plan; it reaches Stripe through the
    // same POST /billing/upgrade the billing console uses. No checkout is
    // created by opening it.
    expect(screen.getByRole("link", { name: /configure your subscription/i }))
      .toHaveAttribute("href", "/subscription/configure");
  });

  it("says plainly that nothing is charged", async () => {
    await reachReady();
    expect(screen.getByText(/no card needed/i)).toBeInTheDocument();
    expect(screen.getByText(/nothing is charged without your authorisation/i)).toBeInTheDocument();
  });

  it("nudges an unverified admin without blocking them", async () => {
    await reachReady({ ...READY, email_verified: false });
    expect(screen.getByText(/verify your email to unlock invites/i)).toBeInTheDocument();
  });
});
