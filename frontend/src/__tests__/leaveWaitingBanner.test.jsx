/**
 * The "Paid leave not yet available" banner on Apply Leave.
 *
 * Reported bug: a new joiner selecting Emergency Leave was shown
 *   🔒 Paid leave not yet available — You become eligible on 2027-01-06
 * with Submit disabled, even though an emergency cannot wait for a tenure
 * milestone. Same for Sick Leave.
 *
 * The banner and the Submit gate are both keyed on the leave type's
 * `waiting_exempt` flag from GET /leave/types, so the actual fix was on the
 * backend (Sick and Emergency now report waiting_exempt: true). These tests
 * pin the frontend half of that contract — that the flag is what the banner
 * obeys — so a refactor here cannot silently re-block an emergency.
 *
 * The employee below is NOT eligible: /leave/eligibility/me says eligible:false
 * in every case. Only the selected type differs.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

const NOT_ELIGIBLE = {
  eligible: false,
  start_date: "2026-07-06",
  waiting_period_months: 6,
  eligible_from: "2027-01-06",
  days_remaining: 133,
  grandfathered: false,
};

const TYPES = [
  // Exempt — day one, like Compensatory and Unpaid.
  { leave_type: "Emergency Leave", icon: "🚨", description: "Sudden emergencies",
    notice_days: 0, annual_limit: 6, doc_required: true, evidence_gate: true,
    waiting_exempt: true, doc_max_mb: 10, doc_accept: ".pdf,.jpg,.jpeg,.png",
    doc_formats: ["PDF", "JPG", "JPEG", "PNG"] },
  { leave_type: "Sick Leave", icon: "🤒", description: "Illness",
    notice_days: 0, annual_limit: 15, doc_required: true, evidence_gate: true,
    waiting_exempt: true, doc_max_mb: 10, doc_accept: ".pdf,.jpg,.jpeg,.png",
    doc_formats: ["PDF", "JPG", "JPEG", "PNG"] },
  // Still gated — the requirement is explicit that these do not change.
  { leave_type: "Annual Leave", icon: "🌴", description: "Planned time off",
    notice_days: 7, annual_limit: 15, doc_required: false, evidence_gate: false,
    waiting_exempt: false },
];

const ROUTES = {
  "/leave/balance": {},
  "/leave/my-requests": [],
  "/leave/types": TYPES,
  "/leave/policies": [],
  "/leave/eligibility/me": NOT_ELIGIBLE,
};

vi.mock("../api/axios", () => ({
  default: {
    request: ({ url }) => {
      const key = Object.keys(ROUTES).find((k) => String(url).startsWith(k));
      return Promise.resolve({ data: key ? ROUTES[key] : {} });
    },
  },
}));

vi.mock("../components/TopNavbar", () => ({ default: () => null }));

import LeavePage from "../pages/LeavePage";
import { LanguageProvider } from "../context/LanguageContext";
import { ThemeProvider } from "../context/ThemeContext";

const BANNER = /Paid leave not yet available/i;

const Wrapped = () => (
  <MemoryRouter>
    <ThemeProvider>
      <LanguageProvider>
        <LeavePage embedded />
      </LanguageProvider>
    </ThemeProvider>
  </MemoryRouter>
);

async function renderAndSelect(leaveType) {
  render(<Wrapped />);
  const select = await screen.findByDisplayValue(/Emergency Leave/i);
  fireEvent.change(select, { target: { value: leaveType } });
  await waitFor(() => expect(select.value).toBe(leaveType));
  return select;
}

beforeEach(() => {
  localStorage.setItem("token", "t");
  localStorage.setItem("role", "employee");
});
afterEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

describe("paid-leave waiting period banner", () => {
  it("is NOT shown for Emergency Leave — this is the reported bug", async () => {
    await renderAndSelect("Emergency Leave");
    await waitFor(() => expect(screen.queryByText(BANNER)).toBeNull());
  });

  it("is NOT shown for Sick Leave", async () => {
    await renderAndSelect("Sick Leave");
    await waitFor(() => expect(screen.queryByText(BANNER)).toBeNull());
  });

  it("IS still shown for Annual Leave — that rule must not change", async () => {
    await renderAndSelect("Annual Leave");
    expect(await screen.findByText(BANNER)).toBeInTheDocument();
  });

  it("does not disable Submit for an emergency the employee cannot postpone", async () => {
    await renderAndSelect("Emergency Leave");
    const submit = await screen.findByRole("button", { name: /Submit Leave Request/i });
    // Emergency Leave carries an evidence gate, so Submit is still disabled
    // until a document is attached — but NOT for the eligibility reason, which
    // relabels the button entirely.
    expect(submit).not.toHaveTextContent(/Not yet eligible/i);
  });

  it("still relabels Submit as ineligible for Annual Leave", async () => {
    await renderAndSelect("Annual Leave");
    // Wait on the banner first: it and the button label are driven by the same
    // `eligibilityBlocks` value, so once the banner is up the state has settled.
    // Querying the button directly raced the /leave/types fetch under load.
    await screen.findByText(BANNER);
    await waitFor(() => {
      expect(screen.getByRole("button", { name: /Not yet eligible/i })).toBeDisabled();
    });
  });
});
