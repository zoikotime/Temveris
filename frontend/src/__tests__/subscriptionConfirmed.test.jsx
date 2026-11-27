/**
 * The screen Stripe returns to after a successful Checkout.
 *
 * The thing worth guarding here is what it is willing to claim. Coming back
 * from Stripe is not proof of payment — a Checkout Session reaches
 * `status: complete` even when `payment_status` is `unpaid` — so the page must
 * say nothing until the server has asked Stripe, and must say something honest
 * when the server refuses.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

globalThis.React = React;

const mockAuthFetch = vi.fn();
vi.mock("../api/authFetch", () => ({
  authFetch: (...a) => mockAuthFetch(...a),
  authFetchJson: vi.fn(),
  default: (...a) => mockAuthFetch(...a),
}));

import SubscriptionConfirmed from "../pages/SubscriptionConfirmed";

const SUBSCRIPTION = {
  plan_name: "Governed",
  billing_cycle: "monthly",
  billable_users: 25,
  active_users: 25,
  currency: "USD",
  renewal_date: "2026-09-24T00:00:00+00:00",
  subscription_status: "active",
  plan: { name: "Governed", monthly_price_per_user: 15, annual_price_per_user: 144 },
};

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const bad = (body, status) => ({ ok: false, status, json: async () => body });

function mockConfirm(response) {
  const calls = [];
  mockAuthFetch.mockImplementation(async (url) => {
    calls.push(String(url));
    if (typeof response === "function") return response(calls.length);
    return response;
  });
  return calls;
}

function renderAt(url = "/subscription/confirmed?session_id=cs_test_123") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/subscription/confirmed" element={<SubscriptionConfirmed />} />
      </Routes>
    </MemoryRouter>,
  );
}

let errorSpy;
beforeEach(() => {
  mockAuthFetch.mockReset();
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
});
afterEach(() => {
  errorSpy.mockRestore();
  vi.restoreAllMocks();
});


// ═══════════════════════════════════════════════════════════════════════════
// It confirms with the server before claiming anything
// ═══════════════════════════════════════════════════════════════════════════
describe("confirming", () => {
  it("asks the existing confirm endpoint, passing the session id", async () => {
    const calls = mockConfirm(ok({ ok: true, subscription: SUBSCRIPTION }));
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });

    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain("/api/admin/billing/upgrade/confirm");
    expect(calls[0]).toContain("session_id=cs_test_123");
  });

  it("claims nothing while the answer is still in flight", async () => {
    mockConfirm(() => new Promise(() => {}));      // never resolves
    renderAt();
    expect(await screen.findByText(/confirming your payment/i)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^payment confirmed$/i })).not.toBeInTheDocument();
  });

  it("confirms once, not twice", async () => {
    // Asking Stripe twice for one session is wasteful and, under React 18's
    // double-mounted effects in development, easy to do by accident.
    const calls = mockConfirm(ok({ ok: true, subscription: SUBSCRIPTION }));
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    await waitFor(() => expect(calls).toHaveLength(1));
  });
});


// ═══════════════════════════════════════════════════════════════════════════
// What it shows once the server has confirmed
// ═══════════════════════════════════════════════════════════════════════════
describe("confirmed", () => {
  beforeEach(() => mockConfirm(ok({ ok: true, subscription: SUBSCRIPTION })));

  it("renders the plan the server reported, not one from the URL", async () => {
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    expect(screen.getByText(/Your Governed subscription is active/i)).toBeInTheDocument();
  });

  it("summarises the subscription from the response", async () => {
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    const row = screen.getByText("Subscription active").closest("li");
    const detail = within(row).getByText(/Governed/);
    expect(detail).toHaveTextContent("Monthly");
    expect(detail).toHaveTextContent("25 seats");
  });

  it("shows the renewal date the server computed from Stripe", async () => {
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    // Formatted for the runner's locale and zone, so the expectation is
    // derived the same way rather than hardcoded to one region's rendering.
    const expected = new Date(SUBSCRIPTION.renewal_date).toLocaleDateString(undefined, {
      year: "numeric", month: "long", day: "numeric",
    });
    expect(screen.getByText((_t, el) =>
      el?.tagName === "STRONG" && el.textContent.includes(expected))).toBeInTheDocument();
    expect(screen.getByText(/Cancel any time/i)).toBeInTheDocument();
  });

  it("shows only the two steps that actually happen", async () => {
    // The reference design also listed entitlements, workspace provisioning and
    // a ready state. None of those run on this path — the workspace is
    // provisioned during signup — so rendering them would be an animation
    // dressed as a status.
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    const timeline = screen.getByRole("list", { name: /what happened/i });
    expect(within(timeline).getAllByRole("listitem")).toHaveLength(2);
    expect(screen.queryByText(/entitlements applying/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/workspace preparing/i)).not.toBeInTheDocument();
  });

  it("offers next steps that all point at routes the product has", async () => {
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    const expected = {
      "Invite your team": "/my-team",
      "Configure policy controls": "/settings",
      "Set up approvals": "/admin/leave",
      "Explore analytics": "/admin",
    };
    for (const [label, href] of Object.entries(expected)) {
      expect(screen.getByRole("link", { name: new RegExp(label, "i") }))
        .toHaveAttribute("href", href);
    }
  });

  it("leads onward into the product and to billing", async () => {
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    expect(screen.getByRole("link", { name: /open temveris/i })).toHaveAttribute("href", "/admin");
    expect(screen.getByRole("link", { name: /view billing/i }))
      .toHaveAttribute("href", "/settings/billing-subscription");
  });

  it("wears the brand mark but no step indicator", async () => {
    // Payment happens after activation has finished; a four-step progress bar
    // here would invent a fifth step.
    renderAt();
    await screen.findByRole("heading", { name: /payment confirmed/i });
    expect(screen.getByAltText("Temveris")).toBeInTheDocument();
    expect(screen.queryByRole("navigation", { name: /activation progress/i })).toBeNull();
  });
});


// ═══════════════════════════════════════════════════════════════════════════
// What it does when the payment was NOT good
// ═══════════════════════════════════════════════════════════════════════════
describe("refused", () => {
  it("never claims success when the server says the payment did not complete", async () => {
    mockConfirm(bad({ detail: "Payment not completed (payment_status=unpaid). Your plan has not been changed." }, 400));
    renderAt();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/couldn't confirm your payment/i);
    expect(alert).toHaveTextContent(/payment_status=unpaid/i);
    expect(screen.queryByRole("heading", { name: /^payment confirmed$/i })).not.toBeInTheDocument();
  });

  it("surfaces a cross-tenant refusal without claiming a charge", async () => {
    mockConfirm(bad({ detail: "This checkout session belongs to another organization." }, 403));
    renderAt();
    expect(await screen.findByRole("alert"))
      .toHaveTextContent(/belongs to another organization/i);
  });

  it("does not claim success when the server is unreachable", async () => {
    mockAuthFetch.mockRejectedValue(new Error("offline"));
    renderAt();
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/couldn't reach the billing service/i);
    expect(screen.queryByRole("heading", { name: /^payment confirmed$/i })).not.toBeInTheDocument();
  });

  it("explains itself when Stripe sent no session reference", async () => {
    renderAt("/subscription/confirmed");
    expect(await screen.findByRole("alert")).toHaveTextContent(/needs a checkout reference/i);
    expect(mockAuthFetch).not.toHaveBeenCalled();
  });

  it("always leaves a route to the authoritative billing page", async () => {
    mockConfirm(bad({ detail: "Payment not completed." }, 400));
    renderAt();
    await screen.findByRole("alert");
    expect(screen.getByRole("link", { name: /open billing page/i }))
      .toHaveAttribute("href", "/settings/billing-subscription");
  });
});
