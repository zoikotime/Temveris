/**
 * Smoke tests for the offboarding primitives — accessibility + status mapping.
 *
 * These are deliberately small: each guards an invariant a future regression
 * could silently break (status label, blocker count, SLA tone, role hint).
 */
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import {
  BlockerChip, RiskBadge, SLAIndicator, StatusChip, EmptyState,
} from "../components/offboarding/primitives";

describe("StatusChip", () => {
  it("maps the canonical status to a label and role=status", () => {
    render(<StatusChip status="access_revocation_failed" />);
    const el = screen.getByRole("status");
    expect(el).toHaveTextContent(/revocation failed/i);
  });
});

describe("BlockerChip", () => {
  it("shows zero state when no blockers", () => {
    render(<BlockerChip count={0} />);
    expect(screen.getByText(/no blockers/i)).toBeInTheDocument();
  });
  it("pluralises correctly", () => {
    render(<BlockerChip count={3} />);
    expect(screen.getByLabelText(/3 open blockers/i)).toBeInTheDocument();
  });
});

describe("SLAIndicator", () => {
  it("flags past-due as breached", () => {
    const past = new Date(Date.now() - 4 * 3600_000).toISOString();
    render(<SLAIndicator dueAt={past} />);
    expect(screen.getByRole("status")).toHaveTextContent(/breached/i);
  });
  it("handles missing due date gracefully", () => {
    render(<SLAIndicator dueAt={null} />);
    expect(screen.getByText(/no sla/i)).toBeInTheDocument();
  });
});

describe("RiskBadge", () => {
  it("renders the level with an aria-label", () => {
    render(<RiskBadge risk="high" />);
    expect(screen.getByLabelText(/risk level: high/i)).toBeInTheDocument();
  });
});

describe("EmptyState", () => {
  it("renders title + hint", () => {
    render(<EmptyState title="Queue is clear" hint="No open incidents" />);
    expect(screen.getByText(/queue is clear/i)).toBeInTheDocument();
    expect(screen.getByText(/no open incidents/i)).toBeInTheDocument();
  });
});
