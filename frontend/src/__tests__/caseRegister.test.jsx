/**
 * Dashboard-workflow tests (§2 register, §9 UX states).
 *
 * Drives the Offboarding Register against a mocked API and asserts the three
 * triage axes (search, saved-filter chips, pagination) each re-query the
 * backend with the right params, that rows are keyboard/click openable, and
 * that the error state surfaces a Retry affordance.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("react-hot-toast", () => ({ default: { success: vi.fn(), error: vi.fn() } }));
vi.mock("../api/offboarding", async () => {
  const actual = await vi.importActual("../api/offboarding");
  return { ...actual, listCases: vi.fn() };
});

import CaseRegister from "../components/offboarding/CaseRegister";
import { listCases } from "../api/offboarding";

const sampleRows = (n = 2) => ({
  items: Array.from({ length: n }, (_, i) => ({
    id: `case_${i}`,
    engagement_id: `ENG-${i}`,
    worker_id: 100 + i,
    status: i === 0 ? "in_progress" : "blocked",
    exit_type: "voluntary",
    department: "Engineering",
    legal_hold_status: "none",
    risk_level: "high",
    final_access_at: null,
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  })),
  total: n,
  limit: 25,
  offset: 0,
});

const renderReg = (props = {}) =>
  render(
    <MemoryRouter>
      <CaseRegister onOpen={props.onOpen || vi.fn()} onCreate={props.onCreate || vi.fn()} />
    </MemoryRouter>
  );

describe("Offboarding Register workflow", () => {
  beforeEach(() => { listCases.mockReset(); listCases.mockResolvedValue(sampleRows(2)); });

  it("loads and renders case rows with status chips", async () => {
    renderReg();
    await waitFor(() => expect(screen.getByText("ENG-0")).toBeInTheDocument());
    expect(screen.getByText("ENG-1")).toBeInTheDocument();
    // Status chips carry a unique aria-label ("Case status: …") so they don't
    // collide with the same words in the status <select> options.
    expect(screen.getByLabelText(/case status: in progress/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/case status: blocked/i)).toBeInTheDocument();
  });

  it("opens a case on row click", async () => {
    const onOpen = vi.fn();
    renderReg({ onOpen });
    await waitFor(() => expect(screen.getByText("ENG-0")).toBeInTheDocument());
    fireEvent.click(screen.getByText("ENG-0"));
    expect(onOpen).toHaveBeenCalledWith("case_0");
  });

  it("opens a case on Enter key (keyboard nav)", async () => {
    const onOpen = vi.fn();
    renderReg({ onOpen });
    await waitFor(() => expect(screen.getByText("ENG-1")).toBeInTheDocument());
    const row = screen.getByRole("link", { name: /open case ENG-1/i });
    fireEvent.keyDown(row, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("case_1");
  });

  it("re-queries with status filter when a saved-filter chip is clicked", async () => {
    renderReg();
    await waitFor(() => expect(listCases).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("tab", { name: "Blocked" }));
    await waitFor(() =>
      expect(listCases).toHaveBeenLastCalledWith(expect.objectContaining({ status: "blocked" }))
    );
  });

  it("re-queries with engagement_id when searching", async () => {
    renderReg();
    await waitFor(() => expect(listCases).toHaveBeenCalled());
    fireEvent.change(screen.getByLabelText(/search by engagement id/i), { target: { value: "ENG-9" } });
    await waitFor(() =>
      expect(listCases).toHaveBeenLastCalledWith(expect.objectContaining({ engagement_id: "ENG-9" }))
    );
  });

  it("disables Next when there is only one page", async () => {
    listCases.mockResolvedValue(sampleRows(2)); // total 2 <= limit 25
    renderReg();
    await waitFor(() => expect(screen.getByText("ENG-0")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /next page/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /previous page/i })).toBeDisabled();
  });

  it("shows an error message AND a Retry button when the API fails", async () => {
    listCases.mockRejectedValueOnce({ message: "boom" });
    renderReg();
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/boom/i));
    const retry = screen.getByRole("button", { name: /retry loading cases/i });
    // Retry re-queries (this time succeeds).
    listCases.mockResolvedValue(sampleRows(1));
    fireEvent.click(retry);
    await waitFor(() => expect(screen.getByText("ENG-0")).toBeInTheDocument());
  });
});
