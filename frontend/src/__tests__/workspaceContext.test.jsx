/**
 * WorkspaceContext regression tests — "Company & Workspace Profile hangs".
 *
 * WorkspaceProfileTab shows its loading skeleton while
 * (!workspace && !loadFailed). The blocker bug was that fetchSettings had
 * terminal paths that left BOTH those flags in that state forever:
 *   • a stale super_admin early-return (GET /org/settings never actually 403s)
 *   • the 401/403 branch cleared workspace but never set loadFailed
 * Either one pinned the page on the skeleton = perpetual blank.
 *
 * These lock in: every settled fetch ends with EITHER workspace set OR
 * loadFailed true — never both false.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// useAuth just needs to hand WorkspaceProvider a truthy token.
vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ token: "test-token" }),
}));

// A 401 now goes through the silent-refresh path (api/refresh.js → axios), so
// the refresh outcome has to be pinned or the test makes a real XHR. Rejecting
// WITHOUT a response models a transient failure: refreshAccessToken() returns
// null, the session is left intact, and the original 401 stands — which is the
// case this file cares about (a 401 must surface as `failed`, never as `hung`).
// See workspaceProfileExpiredToken.test.jsx for the successful-refresh path.
vi.mock("axios", () => ({
  default: { post: vi.fn(async () => { throw new Error("refresh unreachable"); }) },
}));

import { WorkspaceProvider, useWorkspace } from "../context/WorkspaceContext";

function Probe() {
  const { workspace, loadFailed } = useWorkspace();
  // "hung" == the exact skeleton condition in WorkspaceProfileTab.
  const state = workspace ? "loaded" : loadFailed ? "failed" : "hung";
  return <div data-testid="state">{state}</div>;
}

const renderProbe = () =>
  render(
    <WorkspaceProvider>
      <Probe />
    </WorkspaceProvider>,
  );

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  localStorage.setItem("zt_token", "test-token");
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("WorkspaceContext never hangs on the loading skeleton", () => {
  it("resolves to loaded on 200", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ company_name: "Acme" }),
    }));
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("loaded"));
  });

  it("resolves to failed (not hung) on 401", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: false, status: 401, json: async () => ({}),
    }));
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("failed"));
  });

  it("does NOT skip the fetch for super_admin (would hang forever)", async () => {
    localStorage.setItem("role", "super_admin");
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true, status: 200, json: async () => ({ company_name: "Temveris" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    renderProbe();
    await waitFor(() => expect(screen.getByTestId("state")).toHaveTextContent("loaded"));
    expect(fetchMock).toHaveBeenCalled();
  });
});
