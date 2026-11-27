/**
 * Regression: "Could not load workspace profile" appearing intermittently.
 *
 * Company & Workspace Profile reads GET /org/settings through WorkspaceContext,
 * which used raw `fetch`. Raw fetch bypasses the axios response interceptor —
 * the one place that turns a 401 from an expired short-lived access token into
 * a silent refresh + replay. So opening the settings page with an already
 * expired access token produced a hard error screen while the session was in
 * fact perfectly alive (and every axios-based screen kept working).
 *
 * That is the intermittency: it depends purely on whether the access token
 * happened to have expired by the time the page was opened.
 *
 * These tests pin the recovered behaviour: an expired token must resolve to
 * LOADED (not "failed"), and a transient network blip must not leave the page
 * stuck either.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// Mirrors the real AuthContext closely enough to catch a refetch loop: it
// tracks silent token rotation, so `token` here CHANGES whenever
// api/refresh.js announces a new access token — exactly like production.
const authState = { token: "expired-token" };
vi.mock("../context/AuthContext", () => {
  const React = require("react");
  return {
    useAuth: () => {
      const [token, setToken] = React.useState(authState.token);
      React.useEffect(() => {
        const onRefreshed = (e) => {
          authState.token = e.detail.token;
          setToken(e.detail.token);
        };
        window.addEventListener("zt:token-refreshed", onRefreshed);
        return () => window.removeEventListener("zt:token-refreshed", onRefreshed);
      }, []);
      return { token };
    },
  };
});

// Stand in for the HttpOnly-cookie refresh round-trip. api/refresh.js calls
// axios.post("/auth/refresh") directly, so mocking axios is what lets us
// exercise the real refresh-and-replay path.
// Each refresh mints a DISTINCT token, so a rotation-driven refetch loop shows
// up as a runaway call count instead of being masked by an identical string.
const refreshCalls = { count: 0 };
vi.mock("axios", () => ({
  default: {
    post: vi.fn(async () => {
      refreshCalls.count += 1;
      return { data: { access_token: `fresh-token-${refreshCalls.count}` } };
    }),
  },
}));

import { WorkspaceProvider, useWorkspace } from "../context/WorkspaceContext";

function Probe() {
  const { workspace, loadFailed } = useWorkspace();
  const state = workspace ? "loaded" : loadFailed ? "failed" : "pending";
  return (
    <div>
      <div data-testid="state">{state}</div>
      <div data-testid="name">{workspace?.company_name || ""}</div>
    </div>
  );
}

const renderProbe = () =>
  render(
    <WorkspaceProvider>
      <Probe />
    </WorkspaceProvider>,
  );

const ok = (body) => ({ ok: true, status: 200, json: async () => body });

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("zt_token", "expired-token");
  refreshCalls.count = 0;
  authState.token = "expired-token";
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("expired access token on the workspace profile page", () => {
  it("refreshes silently and replays the request instead of showing the error screen", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: false, status: 401, json: async () => ({}) })
      .mockResolvedValueOnce(ok({ company_name: "Acme" }));
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("loaded"),
    );
    expect(screen.getByTestId("name")).toHaveTextContent("Acme");
    expect(refreshCalls.count).toBe(1);      // exactly one refresh
    expect(fetchMock).toHaveBeenCalledTimes(2); // original + replay
  });

  it("sends the ROTATED token on the replay, not the expired one", async () => {
    const seen = [];
    const fetchMock = vi.fn(async (_url, init) => {
      seen.push(new Headers(init.headers).get("Authorization"));
      return seen.length === 1
        ? { ok: false, status: 401, json: async () => ({}) }
        : ok({ company_name: "Acme" });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("loaded"),
    );
    expect(seen[0]).toBe("Bearer expired-token");
    expect(seen[1]).toBe("Bearer fresh-token-1");
  });

  it("reads a token held in sessionStorage (remember-me unchecked)", async () => {
    // The old code read localStorage only, so a token parked in sessionStorage
    // went out as `Bearer null` and 401'd on EVERY visit.
    localStorage.clear();
    sessionStorage.setItem("zt_token", "session-tier-token");
    const seen = [];
    const fetchMock = vi.fn(async (_url, init) => {
      seen.push(new Headers(init.headers).get("Authorization"));
      return ok({ company_name: "Acme" });
    });
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("loaded"),
    );
    expect(seen[0]).toBe("Bearer session-tier-token");
  });

  it("does NOT loop when a 401 is permanent but /auth/refresh stays healthy", async () => {
    // The dangerous shape: get_current_user has 401 paths a fresh access token
    // cannot fix (bad decode, missing user_id claim, deleted user row). Each
    // refresh succeeds and rotates the token, so if anything re-fetches on a
    // token-string change, this becomes an unbounded /auth/refresh storm.
    // Settling on `failed` with a bounded call count is the whole contract.
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false, status: 401, json: async () => ({}),
    });
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("failed"),
    );
    // Give any runaway effect several ticks to spin before we assert.
    await new Promise((r) => setTimeout(r, 400));
    expect(refreshCalls.count).toBe(1);          // one refresh, not a storm
    expect(fetchMock).toHaveBeenCalledTimes(2);  // original + single replay
  });

  it("retries a transient network failure before declaring failure", async () => {
    const fetchMock = vi.fn()
      .mockRejectedValueOnce(new Error("network down"))
      .mockResolvedValueOnce(ok({ company_name: "Acme" }));
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    await waitFor(() =>
      expect(screen.getByTestId("state")).toHaveTextContent("loaded"),
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("keeps the cached copy on screen when the network is genuinely down", async () => {
    // A blip must not blank a good last-known workspace — the page should show
    // real data, not the error screen, whenever a cached copy exists.
    sessionStorage.setItem(
      "zt_workspace_settings",
      JSON.stringify({ company_name: "Cached Co" }),
    );
    const fetchMock = vi.fn().mockRejectedValue(new Error("offline"));
    vi.stubGlobal("fetch", fetchMock);

    renderProbe();

    // Wait until the request has exhausted its retry, so we are asserting on
    // the settled failure state rather than on the first render.
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("state")).toHaveTextContent("loaded");
    expect(screen.getByTestId("name")).toHaveTextContent("Cached Co");
  });
});
