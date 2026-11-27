/**
 * Configure your subscription — the checkout configurator.
 *
 * The single property worth guarding here is that the page decides nothing
 * about money. Every figure in the order summary is rendered from the quote the
 * server returned, and the plan-limit modal is rendered from the server's 409
 * body. So the tests deliberately feed back values that are *wrong* for the
 * seat count on screen: if the page were doing its own arithmetic, it would
 * "correct" them and fail.
 */

import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within, fireEvent } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";

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

import SubscriptionConfigure from "../pages/SubscriptionConfigure";

// ── Fixtures ─────────────────────────────────────────────────────────────────
const PLANS = [
  { code: "verified", name: "Verified", tier_order: 1, monthly_price_per_user: 8,
    annual_price_per_user: 76.8, annual_discount_percentage: 20, user_limit: 25,
    is_enterprise: false, is_most_popular: false, active_status: true, currency: "USD",
    short_description: "Verified work records for small teams." },
  { code: "governed", name: "Governed", tier_order: 2, monthly_price_per_user: 15,
    annual_price_per_user: 144, annual_discount_percentage: 20, user_limit: 250,
    is_enterprise: false, is_most_popular: true, active_status: true, currency: "USD",
    short_description: "Everything in Verified, plus advanced controls, approvals and reporting." },
  { code: "sovereign", name: "Sovereign", tier_order: 3, monthly_price_per_user: 25,
    annual_price_per_user: 240, annual_discount_percentage: 20, user_limit: null,
    is_enterprise: false, is_most_popular: false, active_status: true, currency: "USD",
    short_description: "Multi-jurisdiction compliance infrastructure." },
  { code: "enterprise", name: "Enterprise", tier_order: 4, monthly_price_per_user: 0,
    annual_price_per_user: 0, annual_discount_percentage: 0, user_limit: null,
    is_enterprise: true, is_most_popular: false, active_status: true, currency: "USD",
    short_description: "Private architecture and enterprise terms." },
];

const SUMMARY = {
  subscription: { plan_code: "governed", billing_cycle: "monthly", billable_users: 25 },
};

const MONTHLY_QUOTE = {
  quote_id: "qot_1", plan: "governed", billing_cycle: "monthly", seat_count: 25,
  currency: "USD", line_items: [], subtotal_cents: 37500, discount_cents: 0,
  tax_cents: 0, tax_state: "estimated", total_cents: 37500,
  renewal_amount_cents: 37500, renewal_at: "2026-09-21T00:00:00+00:00",
  state: "active", expires_at: "2026-08-24T12:00:00+00:00",
};

const ANNUAL_QUOTE = {
  ...MONTHLY_QUOTE, billing_cycle: "annual", subtotal_cents: 450000,
  discount_cents: 90000, total_cents: 360000, renewal_amount_cents: 360000,
  renewal_at: "2027-08-21T00:00:00+00:00",
};

const LIMIT_409 = {
  detail: {
    code: "PLAN_LIMIT_EXCEEDED",
    message: "Governed supports up to 250 seats. You entered 300 seats.",
    requested_seats: 300,
    current_plan: { code: "governed", name: "Governed", user_limit: 250,
                    monthly_price_per_user: 15, annual_price_per_user: 144,
                    is_enterprise: false, currency: "USD", badge: "Recommended",
                    short_description: "Policy-enforced workforce intelligence." },
    upgrade_plan: { code: "sovereign", name: "Sovereign", user_limit: null,
                    monthly_price_per_user: 25, annual_price_per_user: 240,
                    is_enterprise: false, currency: "USD", badge: "For regulated and multi-country workforces",
                    short_description: "Advanced governance, evidence and multi-jurisdiction controls." },
    enterprise_plan: { code: "enterprise", name: "Enterprise", user_limit: null,
                       monthly_price_per_user: 0, annual_price_per_user: 0,
                       is_enterprise: true, currency: "USD", badge: "For complex and strategic deployments",
                       short_description: "Private architecture, tailored integrations and enterprise terms." },
  },
};

const ok = (body) => ({ ok: true, status: 200, json: async () => body });
const err = (status, body) => ({ ok: false, status, json: async () => body });

/** @param upgrade  response (or fn) for POST /billing/upgrade */
function mockApi({ quote = MONTHLY_QUOTE, upgrade, plans = PLANS, summary = SUMMARY } = {}) {
  const calls = { quotes: [], upgrades: [] };
  mockAuthFetch.mockImplementation(async (url, init) => {
    const u = String(url);
    if (u.includes("/billing/plans")) return ok({ plans });
    if (u.includes("/billing/summary")) return ok(summary);
    if (u.includes("/api/v1/billing/quote")) {
      const body = JSON.parse(init.body);
      calls.quotes.push(body);
      const q = typeof quote === "function" ? quote(body) : quote;
      return q === "error" ? err(400, {}) : ok(q);
    }
    if (u.includes("/billing/upgrade")) {
      const body = JSON.parse(init.body);
      calls.upgrades.push(body);
      if (!upgrade) return ok({ ok: true, url: "https://checkout.stripe.test/s/x" });
      return typeof upgrade === "function" ? upgrade(body) : upgrade;
    }
    throw new Error(`unexpected fetch: ${u}`);
  });
  return calls;
}

function renderPage(url = "/subscription/configure") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/subscription/configure" element={<SubscriptionConfigure />} />
      </Routes>
    </MemoryRouter>,
  );
}

/** The Today's-total figure. Labelled in the component because the same amount
 *  legitimately appears as the list price when there is no discount or tax.
 *
 *  The element is always in the DOM (it shows an em dash before the first
 *  quote arrives), so `findBy` alone resolves too early — callers pass the
 *  amount they are waiting for. */
async function total(expected) {
  const el = await screen.findByLabelText(/today's total/i);
  if (expected !== undefined) await waitFor(() => expect(el).toHaveTextContent(expected));
  return el;
}

let errorSpy, assignSpy;
beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  mockNavigate.mockClear();
  mockAuthFetch.mockReset();
  errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  assignSpy = vi.fn();
  Object.defineProperty(window, "location", {
    configurable: true, value: { ...window.location, assign: assignSpy },
  });
});
afterEach(() => {
  vi.useRealTimers();
  errorSpy.mockRestore();
  vi.restoreAllMocks();
});


// ═════════════════════════════════════════════════════════════════════════════
// Rendering from the catalog
// ═════════════════════════════════════════════════════════════════════════════
describe("configuration", () => {
  it("shows the plan the subscription is already on", async () => {
    mockApi();
    renderPage();
    expect(await screen.findByText(/Temveris Governed/)).toBeInTheDocument();
    expect(screen.getByText(/Everything in Verified, plus advanced controls/)).toBeInTheDocument();
  });

  it("seeds the seat count from the organization's billable users", async () => {
    mockApi();
    renderPage();
    const seats = await screen.findByLabelText(/^active workers$/i);
    expect(seats).toHaveValue(25);
  });

  it("renders per-seat prices from the catalog, not from constants", async () => {
    mockApi({ plans: PLANS.map((p) =>
      p.code === "governed" ? { ...p, monthly_price_per_user: 19, annual_price_per_user: 182.4 } : p) });
    renderPage();
    expect(await screen.findByText(/\$19\.00 \/active worker \/ month/)).toBeInTheDocument();
    expect(screen.getByText(/\$182\.40 \/active worker \/ year/)).toBeInTheDocument();
  });

  it("shows the annual saving from the catalog's discount percentage", async () => {
    mockApi();
    renderPage();
    expect(await screen.findByText("Save 20%")).toBeInTheDocument();
  });

  it("steps the seat count up and down", async () => {
    mockApi();
    renderPage();
    const seats = await screen.findByLabelText(/^active workers$/i);
    fireEvent.click(screen.getByRole("button", { name: /increase active workers/i }));
    expect(seats).toHaveValue(26);
    fireEvent.click(screen.getByRole("button", { name: /decrease active workers/i }));
    expect(seats).toHaveValue(25);
  });

  it("never lets the seat count fall below one", async () => {
    mockApi({ summary: { subscription: { plan_code: "governed", billable_users: 1 } } });
    renderPage();
    await screen.findByLabelText(/^active workers$/i);
    expect(screen.getByRole("button", { name: /decrease active workers/i })).toBeDisabled();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The order summary is the server's answer
// ═════════════════════════════════════════════════════════════════════════════
describe("order summary", () => {
  it("renders the monthly quote the server returned", async () => {
    mockApi();
    renderPage();
    await total("$375.00");
    // Locale-formatted by the page, so assert the parts rather than an order.
    const renews = screen.getByText(/Renews on/);
    for (const part of ["September", "21", "2026"]) {
      expect(renews).toHaveTextContent(part);
    }
  });

  it("renders the annual quote, including the discount line", async () => {
    mockApi({ quote: (b) => (b.billing_cycle === "annual" ? ANNUAL_QUOTE : MONTHLY_QUOTE) });
    renderPage();
    await total("$375.00");

    fireEvent.click(screen.getByRole("radio", { name: /annual/i }));

    await total("$3,600.00");
    expect(screen.getByText(/annual discount \(20%\)/i)).toBeInTheDocument();
    expect(screen.getByText("-$900.00")).toBeInTheDocument();
    expect(screen.getByText("$4,500.00")).toBeInTheDocument();
  });

  it("displays the server's total even when it contradicts the seats on screen", async () => {
    // The page must not 'correct' this to 25 x $15. If it did, it would be
    // pricing the order itself.
    mockApi({ quote: { ...MONTHLY_QUOTE, total_cents: 99900, subtotal_cents: 99900 } });
    renderPage();
    await total("$999.00");
  });

  it("re-quotes when the seat count changes and sends the new count", async () => {
    const calls = mockApi();
    renderPage();
    await total("$375.00");

    fireEvent.click(screen.getByRole("button", { name: /increase active workers/i }));
    await waitFor(() => expect(calls.quotes.at(-1).seat_count).toBe(26));
  });

  it("re-quotes when the billing cycle changes", async () => {
    const calls = mockApi({ quote: (b) => (b.billing_cycle === "annual" ? ANNUAL_QUOTE : MONTHLY_QUOTE) });
    renderPage();
    await total("$375.00");

    fireEvent.click(screen.getByRole("radio", { name: /annual/i }));
    await waitFor(() => expect(calls.quotes.at(-1).billing_cycle).toBe("annual"));
  });

  it("reports a pricing failure instead of showing a stale total", async () => {
    mockApi({ quote: "error" });
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/couldn't price that configuration/i);
    expect(screen.queryByLabelText(/today's total/i)).not.toBeInTheDocument();
  });

  it("disables payment until a quote exists", async () => {
    mockApi({ quote: "error" });
    renderPage();
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: /proceed to payment/i })).toBeDisabled();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Checkout
// ═════════════════════════════════════════════════════════════════════════════
describe("proceed to payment", () => {
  it("sends the plan, cycle and seat count, and redirects to Stripe", async () => {
    const calls = mockApi();
    renderPage();
    await total("$375.00");

    fireEvent.click(screen.getByRole("button", { name: /proceed to payment/i }));

    await waitFor(() => expect(calls.upgrades).toHaveLength(1));
    expect(calls.upgrades[0]).toEqual({
      plan_code: "governed", billing_cycle: "monthly", seats: 25,
    });
    await waitFor(() =>
      expect(assignSpy).toHaveBeenCalledWith("https://checkout.stripe.test/s/x"));
  });

  it("never sends a price or amount to the server", async () => {
    const calls = mockApi();
    renderPage();
    await total("$375.00");
    fireEvent.click(screen.getByRole("button", { name: /proceed to payment/i }));

    await waitFor(() => expect(calls.upgrades).toHaveLength(1));
    const keys = Object.keys(calls.upgrades[0]);
    for (const forbidden of ["price", "price_id", "amount", "total", "total_cents", "quote_id"]) {
      expect(keys).not.toContain(forbidden);
    }
  });

  it("shows a loading state and blocks a second submit", async () => {
    let release;
    const calls = mockApi({
      upgrade: () => new Promise((r) => { release = () => r(ok({ ok: true, url: "https://x" })); }),
    });
    renderPage();
    await total("$375.00");

    const pay = screen.getByRole("button", { name: /proceed to payment/i });
    fireEvent.click(pay);
    expect(await screen.findByRole("button", { name: /starting checkout/i })).toBeDisabled();
    fireEvent.click(pay);

    release();
    await waitFor(() => expect(calls.upgrades).toHaveLength(1));
  });

  it("reports a checkout failure without implying a charge", async () => {
    mockApi({ upgrade: err(500, { detail: "Billing is not fully configured for this plan." }) });
    renderPage();
    await total("$375.00");
    fireEvent.click(screen.getByRole("button", { name: /proceed to payment/i }));

    expect(await screen.findByText(/billing is not fully configured/i)).toBeInTheDocument();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("navigates to billing when the server changed the plan in place", async () => {
    mockApi({ upgrade: ok({ ok: true, message: "You're now on Governed (monthly)." }) });
    renderPage();
    await total("$375.00");
    fireEvent.click(screen.getByRole("button", { name: /proceed to payment/i }));

    await waitFor(() =>
      expect(mockNavigate).toHaveBeenCalledWith("/settings/billing-subscription", { replace: true }));
    expect(assignSpy).not.toHaveBeenCalled();
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// The plan-limit modal
// ═════════════════════════════════════════════════════════════════════════════
describe("plan limit", () => {
  async function openModal() {
    const calls = mockApi({ upgrade: err(409, LIMIT_409) });
    renderPage();
    await total("$375.00");
    fireEvent.click(screen.getByRole("button", { name: /proceed to payment/i }));
    await screen.findByRole("dialog");
    return calls;
  }

  it("opens only when the server refuses, and renders the server's numbers", async () => {
    await openModal();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/plan limit reached/i)).toBeInTheDocument();
    expect(within(dialog).getByRole("heading", { name: /exceeded the Governed plan limit/i }))
      .toBeInTheDocument();
    expect(within(dialog).getByText(/supports up to 250 seats/)).toBeInTheDocument();
    expect(within(dialog).getByText(/You entered 300 seats/)).toBeInTheDocument();
    expect(within(dialog).getByText(/Limit: 250 seats/)).toBeInTheDocument();
  });

  it("offers the upgrade plan the server named, at the server's price", async () => {
    await openModal();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("SOVEREIGN")).toBeInTheDocument();
    expect(within(dialog).getByText(/\$25\.00 \/ active worker \/ month/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /switch to sovereign/i })).toBeInTheDocument();
  });

  it("offers Enterprise as custom, with no price", async () => {
    await openModal();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("ENTERPRISE")).toBeInTheDocument();
    expect(within(dialog).getByText("Custom")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: /request enterprise demo/i })).toBeInTheDocument();
  });

  it("does not create a checkout session when the limit is hit", async () => {
    await openModal();
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("switching plan re-quotes on the new plan and does not pay yet", async () => {
    const calls = await openModal();
    fireEvent.click(screen.getByRole("button", { name: /switch to sovereign/i }));

    await waitFor(() => expect(calls.quotes.at(-1).plan).toBe("sovereign"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls.upgrades).toHaveLength(1);      // still just the refused one
    expect(assignSpy).not.toHaveBeenCalled();
  });

  it("reduce seats closes the modal and preserves the entered count", async () => {
    await openModal();
    fireEvent.click(screen.getByRole("button", { name: /reduce seats to stay on governed/i }));

    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // Their number is left alone for them to change — not silently reduced.
    expect(screen.getByLabelText(/^active workers$/i)).toHaveValue(25);
  });

  it("enterprise routes to the existing contact-sales flow", async () => {
    await openModal();
    fireEvent.click(screen.getByRole("button", { name: /request enterprise demo/i }));
    expect(mockNavigate).toHaveBeenCalledWith("/contact-sales?reason=enterprise");
  });

  it("cancel dismisses without changing anything", async () => {
    const calls = await openModal();
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(calls.upgrades).toHaveLength(1);
  });

  it("warns inline once the catalog limit is passed, before any submit", async () => {
    const calls = mockApi();
    renderPage();
    const seats = await screen.findByLabelText(/^active workers$/i);
    fireEvent.change(seats, { target: { value: "300" } });

    expect(await screen.findByRole("status"))
      .toHaveTextContent(/Governed supports up to 250 seats/i);
    // A hint only — nothing was submitted, and the server still decides.
    expect(calls.upgrades).toHaveLength(0);
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Arriving from the pricing page's "Subscribe now"
//
// That link carries the plan, cycle and a proposed seat count. All three are a
// starting point for the form — `/upgrade` still clamps seats to the billable
// floor and refuses anything over the plan ceiling, so a hand-edited URL cannot
// buy a configuration the server has not agreed to.
// ═════════════════════════════════════════════════════════════════════════════
describe("seeded from a pricing-page link", () => {
  it("starts at the seat count the link proposed", async () => {
    mockApi();
    renderPage("/subscription/configure?plan=governed&cycle=monthly&seats=25");
    await total("$375.00");
    expect(await screen.findByLabelText(/^active workers$/i)).toHaveValue(25);
  });

  it("starts on the cycle the link proposed", async () => {
    mockApi({ quote: (b) => (b.billing_cycle === "annual" ? ANNUAL_QUOTE : MONTHLY_QUOTE) });
    renderPage("/subscription/configure?plan=governed&cycle=annual&seats=25");
    await total("$3,600.00");
    expect(screen.getByRole("radio", { name: /annual/i })).toHaveAttribute("aria-checked", "true");
  });

  it("falls back to the organisation's own headcount without a seats param", async () => {
    mockApi();
    renderPage("/subscription/configure?plan=governed&cycle=monthly");
    await screen.findByText(/order summary/i);
    await waitFor(() =>
      expect(screen.getByLabelText(/^active workers$/i)).toHaveValue(SUMMARY.subscription.billable_users));
  });

  it("ignores a nonsense seat count in the URL", async () => {
    mockApi();
    renderPage("/subscription/configure?plan=governed&seats=-5");
    await screen.findByText(/order summary/i);
    await waitFor(() =>
      expect(Number(screen.getByLabelText(/^active workers$/i).value)).toBeGreaterThan(0));
  });
});


// ═════════════════════════════════════════════════════════════════════════════
// Which plan is selected when the link does not name one
//
// Reached from Ready's "Configure your subscription", which carries no plan.
// It used to land on whichever tier is badged "Recommended" — Governed, $15 —
// even for a three-person workspace that Verified covers at $8. The catalog's
// own `user_limit` answers it better.
// ═════════════════════════════════════════════════════════════════════════════
describe("default plan", () => {
  const noPlanOn = (billable) =>
    mockApi({ summary: { subscription: { plan_code: null, billing_cycle: "monthly", billable_users: billable } } });

  it("starts on Verified for a team Verified can hold", async () => {
    noPlanOn(3);
    renderPage();
    expect(await screen.findByText(/Temveris Verified/)).toBeInTheDocument();
  });

  it("starts on Governed once the team outgrows Verified", async () => {
    noPlanOn(26);                       // Verified caps at 25
    renderPage();
    expect(await screen.findByText(/Temveris Governed/)).toBeInTheDocument();
  });

  it("starts on Sovereign once the team outgrows Governed", async () => {
    noPlanOn(400);                      // Governed caps at 250
    renderPage();
    expect(await screen.findByText(/Temveris Sovereign/)).toBeInTheDocument();
  });

  it("still honours an explicit plan in the link", async () => {
    // A three-person team that deliberately clicked Governed on the pricing
    // page wants Governed — the features differ, not just the seat ceiling.
    noPlanOn(3);
    renderPage("/subscription/configure?plan=governed");
    expect(await screen.findByText(/Temveris Governed/)).toBeInTheDocument();
  });

  it("still honours the plan the organisation is already on", async () => {
    mockApi({ summary: { subscription: { plan_code: "sovereign", billing_cycle: "monthly", billable_users: 2 } } });
    renderPage();
    expect(await screen.findByText(/Temveris Sovereign/)).toBeInTheDocument();
  });
});
