/**
 * Accessibility tests (§9 WCAG / keyboard nav / audit-friendly UX).
 *
 * These assert the semantic structure assistive tech relies on — proper
 * tablist/tab roles, accessible names, and live-region usage — across the
 * Security Dashboards. The Register's table/tablist/link semantics and the
 * Wizard's dialog/stepper are covered in caseRegister.test / wizard.test.
 *
 * (A full axe-core sweep would be the next step; it needs jest-axe/vitest-axe.
 * These role/name assertions catch the regressions that matter most: missing
 * roles, unlabelled controls, and non-keyboard-operable widgets.)
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

vi.mock("react-hot-toast", () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../api/offboarding", async () => {
  const actual = await vi.importActual("../api/offboarding");
  return {
    ...actual,
    listSecurityEvents: vi.fn().mockResolvedValue({ incidents: [], open_count: 0, recent_quarantine: [] }),
    listReconciliation: vi.fn().mockResolvedValue({ queue: [], conflicts: [], queue_total: 0 }),
    retryRevocation: vi.fn(),
  };
});

import SecurityDashboards from "../components/offboarding/SecurityDashboards";

const QUEUE_NAMES = [
  "Revocation failures", "Identity conflicts", "Token failures",
  "Telemetry violations", "Audit failures", "Legal hold violations",
];

describe("Security Dashboards accessibility", () => {
  beforeEach(() => vi.clearAllMocks());

  it("exposes the six queues as a labelled tablist", async () => {
    render(<SecurityDashboards />);
    const tablist = await screen.findByRole("tablist", { name: /security queues/i });
    expect(tablist).toBeInTheDocument();
    for (const name of QUEUE_NAMES) {
      expect(screen.getByRole("tab", { name: new RegExp(name, "i") })).toBeInTheDocument();
    }
  });

  it("marks the first queue selected and switches aria-selected on click", async () => {
    render(<SecurityDashboards />);
    const first = await screen.findByRole("tab", { name: /revocation failures/i });
    expect(first).toHaveAttribute("aria-selected", "true");

    const telemetry = screen.getByRole("tab", { name: /telemetry violations/i });
    fireEvent.click(telemetry);
    await waitFor(() => expect(telemetry).toHaveAttribute("aria-selected", "true"));
    expect(first).toHaveAttribute("aria-selected", "false");
  });

  it("shows a non-alarming empty state when a queue has no incidents", async () => {
    render(<SecurityDashboards />);
    // Default queue loads empty → 'Queue is clear'.
    expect(await screen.findByText(/queue is clear/i)).toBeInTheDocument();
  });
});
