/**
 * Leave Ledger (admin leave review modal): the Audit & Workflow timeline comes
 * from the audit history without inventing times, the balance keeps the
 * corrected shared-pool numbers, and every action still reaches updateStatus
 * with the same status and comment as the old modal.
 */
import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { makeC } from "../context/ThemeContext";
import LeaveLedgerModal from "../components/leave/LeaveLedgerModal";
import { workflowSteps, policyYearLabel } from "../components/leave/leaveLedgerModel";

const C = makeC(false);

const REQ = {
  id: 197, user_id: 42, name: "Shreelaxmi", email: "shreelaxmi@example.test",
  department: "Technology Infrastructure", leave_type: "Casual Leave",
  from_date: "2026-11-25", to_date: "2026-11-27", days: 3, status: "approved",
  description: "Sister's wedding.", review_comment: "Enjoy", hr_flag: true,
  created_at: "2026-11-11T00:03:59+00:00",
};

const HISTORY = [
  { action: "LeaveRequested",   payload: {},                                   created_at: "2026-11-11T00:03:59+00:00" },
  { action: "LeaveUnderReview", payload: { to_status: "under_review", comment: "Checking cover" }, created_at: "2026-11-11T03:44:27+00:00" },
  { action: "LeaveApproved",    payload: { to_status: "approved", comment: "Enjoy" }, created_at: "2026-11-11T04:32:19+00:00" },
];

// Mid-year joiner: the shared pool is 8.30, this request took 3.
const BALANCES = {
  "Annual Leave": { total: 8.3, accrued: 8.3, used: 0, pending: 0, remaining: 5.3, shared_pool: true },
  "Casual Leave": { total: 15, accrued: 15, used: 3, pending: 0, remaining: 5.3, shared_pool: true },
  "Sick Leave":   { total: 15, accrued: 15, used: 0, pending: 0, remaining: 5.3, shared_pool: true },
  _shared_pool:   { enabled: true, total: 8.3, used: 3, remaining: 5.3, source: "Annual Leave", members: ["Casual Leave", "Sick Leave"] },
};
const BALANCE = { loading: false, error: false, row: BALANCES["Casual Leave"], all: BALANCES, leaveYear: { start: "2026-01-01", end: "2026-12-31", label: "2026" } };

const renderModal = (over = {}) => {
  const props = {
    C, T: {}, request: REQ, statusBadge: <span>Approved</span>, typeIcon: "🗓️",
    balance: BALANCE, history: { loading: false, items: HISTORY, error: false }, workedEvidence: {},
    canAct: true, comment: "", onCommentChange: vi.fn(), onAction: vi.fn(), onClose: vi.fn(),
    onOpenAttachment: vi.fn(), ...over,
  };
  render(<LeaveLedgerModal {...props} />);
  return props;
};

describe("workflowSteps", () => {
  it("follows the audit history with its times and comments", () => {
    const steps = workflowSteps(REQ, HISTORY);
    expect(steps.map((s) => s.kind)).toEqual(["submitted", "under_review", "approved"]);
    expect(steps.map((s) => s.label)).toEqual(["Submitted", "Sent to HR review", "Approved"]);
    expect(steps[1].comment).toBe("Checking cover");
    expect(steps[2].at).toBe("2026-11-11T04:32:19+00:00");
  });

  it("fills a missing submission from the request and never invents a decision time", () => {
    const steps = workflowSteps(REQ, []);
    expect(steps.map((s) => [s.kind, s.at])).toEqual([
      ["submitted", REQ.created_at],
      ["approved", null],
    ]);
    expect(steps[1].comment).toBe("Enjoy");
  });

  it("ends an undecided request with Awaiting decision", () => {
    const steps = workflowSteps({ ...REQ, status: "submitted" }, HISTORY.slice(0, 1));
    expect(steps.map((s) => [s.kind, s.tone])).toEqual([["submitted", "done"], ["awaiting", "pending"]]);
  });

  it("maps generic status changes, keeps edits and skips unknown actions", () => {
    const steps = workflowSteps({ ...REQ, status: "rejected" }, [
      HISTORY[0],
      { action: "LeaveRequestEdited", payload: {}, created_at: "2026-11-11T01:00:00Z" },
      { action: "SomethingElse", payload: {}, created_at: "2026-11-11T01:30:00Z" },
      { action: "LeaveStatusChanged", payload: { to_status: "rejected" }, created_at: "2026-11-11T02:00:00Z" },
    ]);
    expect(steps.map((s) => [s.kind, s.tone])).toEqual([["submitted", "done"], ["edited", "info"], ["rejected", "declined"]]);
  });
});

describe("policyYearLabel", () => {
  it("prefers the server's leave year and falls back to the request's year", () => {
    expect(policyYearLabel({ label: "Apr 2026 – Mar 2027" }, REQ)).toBe("Apr 2026 – Mar 2027");
    expect(policyYearLabel(null, REQ)).toBe("2026");
    expect(policyYearLabel(null, {})).toBe("—");
  });
});

describe("LeaveLedgerModal", () => {
  it("shows the ledger header, details and the timeline on the organization's clock", () => {
    renderModal();
    expect(screen.getByRole("dialog", { name: "Leave Ledger" })).toBeInTheDocument();
    expect(screen.getByTestId("ledger-entry-pill")).toHaveTextContent("Ledger Entry #197");
    const details = within(screen.getByTestId("ledger-details"));
    expect(details.getByText("Shreelaxmi (shreelaxmi@example.test)")).toBeInTheDocument();
    expect(details.getByText("2026-11-25 → 2026-11-27")).toBeInTheDocument();
    expect(details.getByText(/3 Days/)).toBeInTheDocument();
    const timeline = within(screen.getByTestId("ledger-timeline"));
    expect(timeline.getByText("Approved")).toBeInTheDocument();
    // 00:03:59 UTC is 05:33:59 in the default organization zone (Asia/Kolkata).
    expect(timeline.getAllByText(/11\/11\/2026, 5:33:59\s?am/i).length).toBeGreaterThan(0);
    expect(timeline.getByText("“Checking cover”")).toBeInTheDocument();
  });

  it("keeps the corrected shared-pool numbers and moves the per-type split to Balance Breakdown", () => {
    renderModal();
    const card = screen.getByTestId("leave-balance-card");
    const stat = (label) => within(card).getByText(label).nextSibling.textContent;
    expect(stat("Shared Entitlement")).toBe("8.30");
    expect(stat("Used")).toBe("3.00");
    expect(stat("Pending")).toBe("0.00");
    expect(stat("Available Balance")).toBe("5.30");          // 8.30 − 3.00 − 0.00
    expect(within(card).queryByText("Accrued")).toBeNull();
    expect(within(card).queryByTestId("leave-balance-pool-breakdown")).toBeNull();

    const breakdown = within(screen.getByTestId("ledger-breakdown"));
    expect(breakdown.getByText("2026")).toBeInTheDocument();     // Policy Year
    expect(breakdown.getByText("#197")).toBeInTheDocument();
    expect(breakdown.getByTestId("leave-balance-pool-breakdown").textContent).toMatch(/Casual Leave3\.00 days/);
  });

  it("locks an approved request for payroll", () => {
    const p = renderModal();
    expect(within(screen.getByTestId("ledger-actions")).getByText("HR Review Flagged")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Lock for Payroll/ }));
    expect(p.onAction).toHaveBeenCalledWith("locked_for_payroll", "Locked for payroll processing");
  });

  it("approves, declines or sends an undecided request to review with the comment", () => {
    const p = renderModal({ request: { ...REQ, status: "submitted" }, comment: "ok" });
    fireEvent.click(screen.getByRole("button", { name: /Approve/ }));
    expect(p.onAction).toHaveBeenLastCalledWith("approved", "ok");
    fireEvent.click(screen.getByRole("button", { name: /Decline/ }));
    expect(p.onAction).toHaveBeenLastCalledWith("rejected", "ok");
    fireEvent.click(screen.getByRole("button", { name: /Send to HR Review/ }));
    expect(p.onAction).toHaveBeenLastCalledWith("under_review", "ok");
    fireEvent.change(screen.getByLabelText(/Comment/), { target: { value: "note" } });
    expect(p.onCommentChange).toHaveBeenCalledWith("note");
    expect(screen.queryByRole("button", { name: /Lock for Payroll/ })).toBeNull();
  });

  it("uses the default review comment when none is typed", () => {
    const p = renderModal({ request: { ...REQ, status: "submitted" }, comment: "" });
    fireEvent.click(screen.getByRole("button", { name: /Send to HR Review/ }));
    expect(p.onAction).toHaveBeenLastCalledWith("under_review", "Marked for review");
  });

  it("is view-only when the plan withholds approvals", () => {
    renderModal({ request: { ...REQ, status: "under_review" }, canAct: false });
    expect(screen.getByText(/View only/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Approve/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Send to HR Review/ })).toBeNull();
  });

  it("disables the actions while one is in flight", () => {
    renderModal({ request: { ...REQ, status: "submitted" }, actioning: true });
    expect(screen.getByRole("button", { name: /Approve/ })).toBeDisabled();
    expect(screen.getByRole("button", { name: /Decline/ })).toBeDisabled();
  });

  it("opens the attachment through the handler", () => {
    const p = renderModal({ request: { ...REQ, has_attachment: true, attachment_name: "note.pdf" } });
    fireEvent.click(screen.getByRole("button", { name: /note\.pdf/ }));
    expect(p.onOpenAttachment).toHaveBeenCalledTimes(1);
  });

  it("closes on the close button, Escape and the backdrop, not on a click inside", () => {
    const p = renderModal();
    fireEvent.click(screen.getByTestId("ledger-details"));
    expect(p.onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    fireEvent.keyDown(document, { key: "Escape" });
    fireEvent.click(screen.getByRole("dialog"));
    expect(p.onClose).toHaveBeenCalledTimes(3);
  });

  it("says when the history could not be loaded and still shows what the request records", () => {
    renderModal({ history: { loading: false, items: null, error: true } });
    expect(screen.getByText(/Full history couldn't be loaded/)).toBeInTheDocument();
    const timeline = within(screen.getByTestId("ledger-timeline"));
    expect(timeline.getByText("Submitted")).toBeInTheDocument();
    expect(timeline.getByText("Time not recorded")).toBeInTheDocument();   // the approval time is unknown
  });
});
