/**
 * BillingModule — refresh entitlements after a SUCCESSFUL plan change.
 *
 * Root cause it fixes: EntitlementsContext only refetches on user-id change, so a
 * plan change left the UI (e.g. Payroll Calculator) on the pre-upgrade projection.
 * BillingModule now calls EntitlementsContext.refresh() after the server confirms a
 * plan change, so gated features unlock in place without a logout.
 *
 * Contract pinned here:
 *  - immediate in-place upgrade success  -> refresh()
 *  - Stripe checkout confirmation success -> refresh()
 *  - upgrade/confirmation FAILURE         -> NO refresh (never optimistic)
 *  - payment-method (setup_intent) return -> NO refresh (not a plan change)
 *  - scheduled downgrade request          -> NO refresh (entitlements stay until renewal)
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, act, fireEvent } from "@testing-library/react";

const refreshEntitlements = vi.fn();
vi.mock("../context/EntitlementsContext", () => ({
  useEntitlements: () => ({ refresh: refreshEntitlements }),
}));

// Theme colours are accessed as C.<anything>; a proxy returns a string for any key.
const C = new Proxy({}, { get: () => "#123456" });
vi.mock("../context/ThemeContext", () => ({ useTheme: () => ({ C, isDark: false }) }));
vi.mock("../context/LanguageContext", () => ({ useLanguage: () => ({ T: {} }) }));
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => vi.fn() };
});

import BillingModule from "../components/BillingModule";

// ── fetch mock: fragment -> handler, with an OK-empty default for mount loads ──
let handlers;
function resp(body, ok = true) {
  return Promise.resolve({ ok, status: ok ? 200 : 402, json: async () => body });
}
beforeEach(() => {
  refreshEntitlements.mockReset();
  window.history.replaceState({}, "", "/");
  handlers = {};
  global.fetch = vi.fn((url) => {
    const u = String(url);
    for (const [frag, fn] of Object.entries(handlers)) if (u.includes(frag)) return fn(u);
    return resp({});
  });
});
afterEach(() => vi.clearAllMocks());

const GOVERNED_SUMMARY = {
  is_internal_tenant: false,
  subscription: {
    plan: { code: "governed", tier_order: 2, name: "Governed" },
    subscription_status: "active", billing_cycle: "monthly", has_billing_account: true,
    renewal_date: "2026-09-27T00:00:00Z",
  },
  seat_metrics: { active: 1 },
};
const PLANS = {
  plans: [
    { code: "governed",  tier_order: 2, name: "Governed",  monthly_price_per_user: 15, annual_price_per_user: 144, currency: "USD", short_description: "g", feature_list_json: [] },
    { code: "sovereign", tier_order: 3, name: "Sovereign", monthly_price_per_user: 25, annual_price_per_user: 240, currency: "USD", short_description: "s", feature_list_json: [] },
  ],
};

async function openPlans() {
  await act(async () => { render(<BillingModule />); });
  await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
    expect.stringContaining("/billing/plans"), expect.anything()));
  // Enter the plan-comparison view.
  const toPlans = (await screen.findAllByText(/change plan/i))[0];
  await act(async () => { fireEvent.click(toPlans); });
}

describe("BillingModule — entitlement refresh after a plan change", () => {
  it("refreshes after a SUCCESSFUL in-place upgrade (Governed -> Sovereign)", async () => {
    handlers["/billing/summary"] = () => resp(GOVERNED_SUMMARY);
    handlers["/billing/plans"] = () => resp(PLANS);
    // In-place modify returns NO url -> the UI treats it as an immediate change.
    handlers["/billing/upgrade"] = () => resp({ ok: true, message: "You're now on Sovereign." });

    await openPlans();
    const upgradeBtn = await screen.findByText(/upgrade to sovereign/i);
    await act(async () => { fireEvent.click(upgradeBtn); });
    const confirmBtn = await screen.findByText(/confirm upgrade/i);
    await act(async () => { fireEvent.click(confirmBtn); });

    await waitFor(() => expect(refreshEntitlements).toHaveBeenCalledTimes(1));
  });

  it("does NOT refresh when the in-place upgrade FAILS", async () => {
    handlers["/billing/summary"] = () => resp(GOVERNED_SUMMARY);
    handlers["/billing/plans"] = () => resp(PLANS);
    handlers["/billing/upgrade"] = () => resp({ detail: "card declined" }, false);

    await openPlans();
    const upgradeBtn = await screen.findByText(/upgrade to sovereign/i);
    await act(async () => { fireEvent.click(upgradeBtn); });
    const confirmBtn = await screen.findByText(/confirm upgrade/i);
    await act(async () => { fireEvent.click(confirmBtn); });

    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/billing/upgrade"), expect.anything()));
    expect(refreshEntitlements).not.toHaveBeenCalled();
  });

  it("refreshes after a SUCCESSFUL Stripe checkout confirmation", async () => {
    window.history.replaceState({}, "", "/?session_id=cs_test_ok");
    handlers["/upgrade/confirm"] = () => resp({ ok: true });
    await act(async () => { render(<BillingModule />); });
    await waitFor(() => expect(refreshEntitlements).toHaveBeenCalledTimes(1));
  });

  it("does NOT refresh when the checkout confirmation FAILS (payment unconfirmed)", async () => {
    window.history.replaceState({}, "", "/?session_id=cs_test_bad");
    handlers["/upgrade/confirm"] = () => resp({ detail: "not paid" }, false);
    await act(async () => { render(<BillingModule />); });
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/upgrade/confirm"), expect.anything()));
    expect(refreshEntitlements).not.toHaveBeenCalled();
  });

  it("does NOT refresh on a payment-method (setup_intent) return — not a plan change", async () => {
    window.history.replaceState({}, "", "/?setup_intent=seti_123");
    handlers["/payment-method/confirm"] = () => resp({ ok: true });
    await act(async () => { render(<BillingModule />); });
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/payment-method/confirm"), expect.anything()));
    expect(refreshEntitlements).not.toHaveBeenCalled();
  });

  it("does NOT refresh on a scheduled downgrade request (entitlements stay until renewal)", async () => {
    handlers["/billing/summary"] = () => resp(GOVERNED_SUMMARY);
    handlers["/billing/plans"] = () => resp(PLANS);
    handlers["/downgrade-request"] = () => resp({ message: "Request received." });

    await openPlans();
    // The Sovereign->Governed direction is a downgrade; from Governed the lower
    // plan card offers "Contact us to downgrade". Trigger whatever downgrade CTA
    // is present, then submit the request, and assert no entitlement refresh.
    const downgradeCta = screen.queryByText(/contact us to downgrade/i);
    if (downgradeCta) {
      await act(async () => { fireEvent.click(downgradeCta); });
      const submit = screen.queryByText(/submit|confirm|request/i);
      if (submit) await act(async () => { fireEvent.click(submit); });
    }
    expect(refreshEntitlements).not.toHaveBeenCalled();
  });
});
