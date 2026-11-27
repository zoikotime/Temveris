/**
 * RBAC frontend tests (§6 role-based UI).
 *
 * The backend is authoritative for RBAC: it substitutes "***redacted***" for
 * any sensitive column the caller may not view. The frontend's job is to
 * render that as a neutral "Withheld" pill — never the marker, never a blank
 * cell that hides the fact a value exists. These tests drive CaseDetail with
 * a mocked API response to prove the end-to-end behaviour.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";

// Mock the API module CaseDetail imports. Only getCase matters for the
// Overview tab; the rest are stubbed so imports resolve.
vi.mock("../api/offboarding", async () => {
  const actual = await vi.importActual("../api/offboarding");
  return {
    ...actual,
    getCase: vi.fn(),
    getAudit: vi.fn().mockResolvedValue([]),
    getCertificate: vi.fn().mockRejectedValue({ response: { status: 404 } }),
    listEvidencePackages: vi.fn().mockResolvedValue([]),
    listPolicies: vi.fn().mockResolvedValue([]),
    listDeletionReviews: vi.fn().mockResolvedValue([]),
  };
});

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

import CaseDetail from "../components/offboarding/CaseDetail";
import { getCase } from "../api/offboarding";

const baseCase = (overrides = {}) => ({
  id: "case_abc",
  tenant_id: "default",
  engagement_id: "ENG-1",
  worker_id: 7,
  status: "in_progress",
  blocked_from_state: null,
  exit_type: "voluntary",
  jurisdiction: "EU",
  worker_type: "full_time",
  risk_level: "high",
  department: "Engineering",
  data_classification: "internal",
  legal_hold_status: "none",
  revocation_mode: "scheduled",
  final_access_at: null,
  termination_reason: "role eliminated",
  notes: "n/a",
  correlation_id: "cor_x",
  tasks: [], revocations: [], blockers: [], readiness: null,
  allowed_actions: [],
  ...overrides,
});

describe("RBAC role-based UI", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders a Withheld pill when termination_reason is redacted by the backend", async () => {
    getCase.mockResolvedValue(baseCase({ termination_reason: "***redacted***" }));
    render(<CaseDetail caseId="case_abc" onBack={() => {}} />);

    // The neutral, accessible "Withheld" pill stands in for the redacted value
    // (Overview is the default tab). findBy* waits for the async getCase.
    expect(await screen.findByText(/withheld/i)).toBeInTheDocument();
    // The redaction marker must NEVER be visible.
    expect(screen.queryByText("***redacted***")).toBeNull();
  });

  it("shows the real termination reason when the caller is entitled to it", async () => {
    getCase.mockResolvedValue(baseCase({ termination_reason: "redundancy" }));
    render(<CaseDetail caseId="case_abc" onBack={() => {}} />);
    expect(await screen.findByText("redundancy")).toBeInTheDocument();
    expect(screen.queryByText(/withheld/i)).toBeNull();
  });
});
