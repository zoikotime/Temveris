/**
 * HTTP 402 handling — a suspended customer must be told, and given a way out.
 *
 * Before this the frontend had no handling for 402 at all: `grep 402` across src/
 * returned nothing. The backend suspends a non-paying organization to read-only after
 * the dunning grace period and answers restricted writes with 402, so a customer whose
 * card had failed watched their saves fail with a generic error and no route to fix it.
 */

import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { render, screen, act } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import BillingOverdueBanner from "../components/BillingOverdueBanner";
import {
  BILLING_BLOCKED_EVENT,
  BILLING_RECOVERY_PATH,
  announceBillingBlocked,
  isBillingBlocked,
  onBillingBlocked,
} from "../api/billingBlocked";

const mockNavigate = vi.fn();
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual("react-router-dom");
  return { ...actual, useNavigate: () => mockNavigate };
});

function renderBanner() {
  return render(
    <MemoryRouter>
      <BillingOverdueBanner />
    </MemoryRouter>
  );
}

function fire(detail = {}) {
  act(() => {
    announceBillingBlocked(detail);
  });
}

beforeEach(() => mockNavigate.mockReset());
afterEach(() => vi.clearAllMocks());

describe("the 402 predicate", () => {
  it("treats only 402 as a billing block", () => {
    expect(isBillingBlocked(402)).toBe(true);
    // 401/403 are session and lifecycle problems and route through logout instead —
    // conflating them would sign out a customer who only needs to pay.
    for (const status of [200, 400, 401, 403, 409, 500]) {
      expect(isBillingBlocked(status)).toBe(false);
    }
  });
});

describe("the announcement channel", () => {
  it("delivers the detail to a subscriber", () => {
    const seen = [];
    const off = onBillingBlocked((e) => seen.push(e.detail));
    fire({ message: "Payment overdue", path: "/employees/add-direct" });
    off();
    expect(seen).toHaveLength(1);
    expect(seen[0].message).toBe("Payment overdue");
    expect(seen[0].path).toBe("/employees/add-direct");
  });

  it("unsubscribes cleanly so a remount cannot stack listeners", () => {
    const seen = [];
    const off = onBillingBlocked(() => seen.push(1));
    off();
    fire();
    expect(seen).toHaveLength(0);
  });

  it("never throws when CustomEvent is unavailable", () => {
    const original = global.CustomEvent;
    // A browser without CustomEvent must not turn a billing notice into a second
    // failure on top of the request that already failed.
    global.CustomEvent = undefined;
    expect(() => announceBillingBlocked({ message: "x" })).not.toThrow();
    expect(announceBillingBlocked({ message: "x" })).toBe(false);
    global.CustomEvent = original;
  });
});

describe("BillingOverdueBanner", () => {
  it("renders nothing until a write is actually refused", () => {
    renderBanner();
    // No polling, no guessing from subscription state — so it cannot cry wolf.
    expect(screen.queryByTestId("billing-overdue-banner")).toBeNull();
  });

  it("appears with the backend's message when one is supplied", () => {
    renderBanner();
    fire({ message: "Your workspace is read-only: payment overdue since 4 August." });
    expect(screen.getByTestId("billing-overdue-banner")).toBeTruthy();
    expect(screen.getByText(/read-only: payment overdue since 4 August/)).toBeTruthy();
  });

  it("falls back to its own copy when the backend sends no detail", () => {
    renderBanner();
    fire({});
    expect(screen.getByText(/payment is overdue/i)).toBeTruthy();
  });

  it("offers a route to update the payment method", () => {
    renderBanner();
    fire({});
    act(() => screen.getByTestId("billing-overdue-fix").click());
    expect(mockNavigate).toHaveBeenCalledWith(BILLING_RECOVERY_PATH);
  });

  it("can be dismissed, because an employee cannot fix billing", () => {
    renderBanner();
    fire({});
    act(() => screen.getByTestId("billing-overdue-dismiss").click());
    expect(screen.queryByTestId("billing-overdue-banner")).toBeNull();
  });

  it("comes back on the next refused write after being dismissed", () => {
    renderBanner();
    fire({});
    act(() => screen.getByTestId("billing-overdue-dismiss").click());
    expect(screen.queryByTestId("billing-overdue-banner")).toBeNull();
    fire({ message: "Still overdue" });
    // The information must not be lost just because it was dismissed once.
    expect(screen.getByTestId("billing-overdue-banner")).toBeTruthy();
  });

  it("announces itself to assistive technology", () => {
    renderBanner();
    fire({});
    expect(screen.getByTestId("billing-overdue-banner").getAttribute("role")).toBe("alert");
  });

  it("does not navigate on its own — the customer chooses", () => {
    renderBanner();
    fire({});
    // 402 leaves reads and clock-in/out working, so yanking the user elsewhere would
    // interrupt work that is still permitted.
    expect(mockNavigate).not.toHaveBeenCalled();
  });
});

describe("event name stability", () => {
  it("keeps the documented event name", () => {
    // The name is part of the contract between api/axios.js and the banner.
    expect(BILLING_BLOCKED_EVENT).toBe("zoiko:billing-blocked");
    expect(BILLING_RECOVERY_PATH).toBe("/settings/billing-subscription");
  });
});
