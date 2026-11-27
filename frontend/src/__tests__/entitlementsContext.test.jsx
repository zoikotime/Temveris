/**
 * Phase 3.7 — EntitlementsContext: fetch, actor isolation, safe fallback.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, act } from "@testing-library/react";

// Controllable auth + API mocks.
const authState = { user: { id: 1, role: "admin" } };
vi.mock("../context/AuthContext", () => ({
  useAuth: () => authState,
}));

const apiGet = vi.fn();
vi.mock("../api/axios", () => ({ default: { get: (...a) => apiGet(...a) } }));

import { EntitlementsProvider, useEntitlements } from "../context/EntitlementsContext";

function Probe() {
  const { presentationFor, ready, error } = useEntitlements();
  return (
    <div>
      <span data-testid="ready">{String(ready)}</span>
      <span data-testid="error">{error ? "err" : "none"}</span>
      <span data-testid="reports">{presentationFor("reports.standard") || "null"}</span>
      <span data-testid="api">{presentationFor("integrations.api") || "null"}</span>
    </div>
  );
}

const projectionResponse = (overrides = {}) => ({
  data: {
    capabilities: [
      { capability: "reports.standard", presentation: "show" },
      { capability: "integrations.api", presentation: "lock" },
      ...(overrides.extra || []),
    ],
  },
});

beforeEach(() => {
  apiGet.mockReset();
  authState.user = { id: 1, role: "admin" };
});

describe("EntitlementsContext", () => {
  it("loads the projection and exposes presentation", async () => {
    apiGet.mockResolvedValueOnce(projectionResponse());
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);

    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("true"));
    expect(screen.getByTestId("reports").textContent).toBe("show");
    expect(screen.getByTestId("api").textContent).toBe("lock");
    expect(apiGet).toHaveBeenCalledWith("/api/v1/entitlements/projection");
  });

  it("falls back safely (projection null) when the request fails", async () => {
    apiGet.mockRejectedValueOnce(new Error("boom"));
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);

    await waitFor(() => expect(screen.getByTestId("error").textContent).toBe("err"));
    // ready stays false and presentationFor returns null — consumers then render
    // everything as pre-3.7 (safe: no server enforcement exists).
    expect(screen.getByTestId("ready").textContent).toBe("false");
    expect(screen.getByTestId("reports").textContent).toBe("null");
  });

  it("does not fetch when there is no signed-in user", async () => {
    authState.user = null;
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("false"));
    expect(apiGet).not.toHaveBeenCalled();
  });

  it("clears the previous actor's projection and refetches on user change", async () => {
    apiGet.mockResolvedValueOnce(projectionResponse());   // actor 1: api=lock
    const { rerender } = render(
      <EntitlementsProvider><Probe /></EntitlementsProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("api").textContent).toBe("lock"));

    // Actor 2 (a worker) resolves api as HIDE. Switching users must not retain
    // actor 1's "lock" — the context clears synchronously then refetches.
    apiGet.mockResolvedValueOnce({
      data: { capabilities: [
        { capability: "reports.standard", presentation: "show" },
        { capability: "integrations.api", presentation: "hide" },
      ] },
    });
    act(() => { authState.user = { id: 2, role: "employee" }; });
    rerender(<EntitlementsProvider><Probe /></EntitlementsProvider>);

    await waitFor(() => expect(screen.getByTestId("api").textContent).toBe("hide"));
    expect(apiGet).toHaveBeenCalledTimes(2);
  });

  it("never writes projection to localStorage", async () => {
    const setItem = vi.spyOn(window.localStorage.__proto__, "setItem");
    apiGet.mockResolvedValueOnce(projectionResponse());
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("true"));
    const wroteProjection = setItem.mock.calls.some(
      ([k]) => typeof k === "string" && k.toLowerCase().includes("entitlement"),
    );
    expect(wroteProjection).toBe(false);
    setItem.mockRestore();
  });
});
