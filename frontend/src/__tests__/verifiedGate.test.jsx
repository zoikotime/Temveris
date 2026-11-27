/**
 * VerifiedGate — the route guard that stops an unverified self-serve signup from
 * reaching the product by navigating around the verify step (e.g. the browser
 * Back button from /verify-email, which works because signup hands out a session
 * at Step 1, before the OTP).
 *
 * The property under test is NON-LOCKOUT. The gate must redirect exactly one
 * case — a fresh signup that has a commercial signup row and is not yet verified
 * — and let EVERYONE else through: verified users, existing tenants with no
 * signup row, callers with no commercial org (404), and any request that errors.
 * Each of those is a case where a wrong gate would lock a real admin out of
 * their own dashboard, so each gets an explicit test.
 */
import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// Hoisted so the vi.mock factory below can reference it safely.
const { readToken } = vi.hoisted(() => ({ readToken: vi.fn() }));
vi.mock("../api/authStorage", () => ({ readToken }));
vi.mock("../config", () => ({ API_BASE: "http://test.local" }));

import VerifiedGate from "../components/VerifiedGate";

// Render VerifiedGate at `path`, wrapping a PROTECTED marker; a sibling
// /verify-email route lets us observe a redirect when the gate fires.
function renderAt(path) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/verify-email" element={<div>VERIFY_PAGE</div>} />
        <Route path="*" element={<VerifiedGate><div>PROTECTED</div></VerifiedGate>} />
      </Routes>
    </MemoryRouter>,
  );
}

function mockFetch(impl) {
  const fn = vi.fn(impl);
  vi.stubGlobal("fetch", fn);
  return fn;
}
const jsonOk = (body) =>
  Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });

beforeEach(() => {
  readToken.mockReset();
  readToken.mockReturnValue("tok-123"); // authenticated by default
});
afterEach(() => { vi.unstubAllGlobals(); });

describe("VerifiedGate", () => {
  it("redirects a fresh UNVERIFIED self-serve signup to /verify-email", async () => {
    mockFetch(() => jsonOk({ signup_id: "s1", email_verified: false }));
    renderAt("/admin");
    expect(await screen.findByText("VERIFY_PAGE")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.queryByText("PROTECTED")).not.toBeInTheDocument(),
    );
  });

  it("lets a VERIFIED signup through", async () => {
    const f = mockFetch(() => jsonOk({ signup_id: "s1", email_verified: true }));
    renderAt("/admin");
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(screen.queryByText("VERIFY_PAGE")).not.toBeInTheDocument();
  });

  it("lets an existing tenant with NO signup row through (cannot lock out)", async () => {
    const f = mockFetch(() => jsonOk({ signup_id: null, email_verified: false }));
    renderAt("/admin");
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(screen.queryByText("VERIFY_PAGE")).not.toBeInTheDocument();
  });

  it("lets a caller with no commercial org (404) through", async () => {
    const f = mockFetch(() =>
      Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve(null) }),
    );
    renderAt("/admin");
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(screen.queryByText("VERIFY_PAGE")).not.toBeInTheDocument();
  });

  it("fails OPEN on a network error", async () => {
    const f = mockFetch(() => Promise.reject(new Error("offline")));
    renderAt("/admin");
    await waitFor(() => expect(f).toHaveBeenCalled());
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(screen.queryByText("VERIFY_PAGE")).not.toBeInTheDocument();
  });

  it("does not gate (and does not fetch) when there is no token", async () => {
    readToken.mockReturnValue(null);
    const f = mockFetch(() => jsonOk({ signup_id: "s1", email_verified: false }));
    renderAt("/admin");
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(f).not.toHaveBeenCalled();
  });

  it("does not gate (and does not fetch) on an exempt checkout path", async () => {
    const f = mockFetch(() => jsonOk({ signup_id: "s1", email_verified: false }));
    renderAt("/subscription/configure");
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(f).not.toHaveBeenCalled();
  });

  it("does not gate (and does not fetch) on the exempt /verify-email path itself", async () => {
    const f = mockFetch(() => jsonOk({ signup_id: "s1", email_verified: false }));
    // Wrap /verify-email in the gate directly to prove exemption (no redirect loop).
    render(
      <MemoryRouter initialEntries={["/verify-email"]}>
        <Routes>
          <Route
            path="/verify-email"
            element={<VerifiedGate><div>PROTECTED</div></VerifiedGate>}
          />
        </Routes>
      </MemoryRouter>,
    );
    expect(screen.getByText("PROTECTED")).toBeInTheDocument();
    expect(f).not.toHaveBeenCalled();
  });
});
