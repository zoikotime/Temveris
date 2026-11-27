/**
 * Plan-change entitlement refresh — the mechanism the BillingModule fix relies on.
 *
 * After a successful plan change the user id does NOT change, so the projection is
 * only refreshed if something calls EntitlementsContext.refresh(). These tests pin
 * that refresh() re-fetches the actor projection and flips a capability's
 * presentation in place — e.g. payroll.calculator LOCK -> SHOW when the server now
 * resolves Sovereign — without any logout or user change. The projection always
 * reflects the latest server truth, never a stale snapshot.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";

const authState = { user: { id: 1, role: "admin" } };
vi.mock("../context/AuthContext", () => ({ useAuth: () => authState }));

const apiGet = vi.fn();
vi.mock("../api/axios", () => ({ default: { get: (...a) => apiGet(...a) } }));

import { EntitlementsProvider, useEntitlements } from "../context/EntitlementsContext";

function Probe() {
  const { presentationFor, ready, refresh } = useEntitlements();
  return (
    <div>
      <span data-testid="ready">{String(ready)}</span>
      <span data-testid="payroll">{presentationFor("payroll.calculator") || "null"}</span>
      <button data-testid="refresh" onClick={() => refresh()}>refresh</button>
    </div>
  );
}

const proj = (payroll) => ({
  data: { capabilities: [{ capability: "payroll.calculator", presentation: payroll }] },
});

beforeEach(() => {
  apiGet.mockReset();
  authState.user = { id: 1, role: "admin" };
});

describe("entitlement refresh unlocks in place (plan-change fix)", () => {
  it("flips payroll.calculator LOCK -> SHOW after refresh(), with no login/user change", async () => {
    apiGet.mockResolvedValueOnce(proj("lock"));   // pre-upgrade (Verified) — payroll locked
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);
    await waitFor(() => expect(screen.getByTestId("payroll").textContent).toBe("lock"));

    // Server now resolves Sovereign -> payroll SHOW. The upgrade did NOT change the
    // user id, so only refresh() can surface it.
    apiGet.mockResolvedValueOnce(proj("show"));
    fireEvent.click(screen.getByTestId("refresh"));

    await waitFor(() => expect(screen.getByTestId("payroll").textContent).toBe("show"));
    expect(apiGet).toHaveBeenCalledTimes(2);
    expect(apiGet).toHaveBeenLastCalledWith("/api/v1/entitlements/projection");
  });

  it("re-locks when the server later denies — refresh is authoritative, never stale", async () => {
    apiGet.mockResolvedValueOnce(proj("show"));
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);
    await waitFor(() => expect(screen.getByTestId("payroll").textContent).toBe("show"));

    apiGet.mockResolvedValueOnce(proj("lock"));
    fireEvent.click(screen.getByTestId("refresh"));
    await waitFor(() => expect(screen.getByTestId("payroll").textContent).toBe("lock"));
  });

  it("a failed refresh does not fabricate the new plan's access (safe fallback)", async () => {
    apiGet.mockResolvedValueOnce(proj("lock"));
    render(<EntitlementsProvider><Probe /></EntitlementsProvider>);
    await waitFor(() => expect(screen.getByTestId("payroll").textContent).toBe("lock"));

    // If the refresh request fails, the context clears to the safe null fallback
    // (ready=false, presentationFor -> null). It never invents SHOW for a plan the
    // server did not confirm.
    apiGet.mockRejectedValueOnce(new Error("network"));
    fireEvent.click(screen.getByTestId("refresh"));
    await waitFor(() => expect(screen.getByTestId("ready").textContent).toBe("false"));
    expect(screen.getByTestId("payroll").textContent).toBe("null");
  });
});
