/**
 * Early Clock Out modal (Controlled Clock-Out — Phase 2) tests.
 *
 * Verifies the typed-reason UX contract: free-text only (no dropdowns/radios),
 * required, min 15 / max 1000 chars, trimmed, and the Cancel / Clock Out
 * button wiring.
 */
import React from "react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import EarlyClockOutModal, {
  EARLY_REASON_MIN,
  EARLY_REASON_MAX,
} from "../components/EarlyClockOutModal";

function setup(props = {}) {
  const onReasonChange = vi.fn();
  const onCancel = vi.fn();
  const onSubmit = vi.fn();
  const utils = render(
    <EarlyClockOutModal
      reason=""
      onReasonChange={onReasonChange}
      error=""
      submitting={false}
      onCancel={onCancel}
      onSubmit={onSubmit}
      {...props}
    />
  );
  return { onReasonChange, onCancel, onSubmit, ...utils };
}

describe("EarlyClockOutModal", () => {
  it("shows the title, message and a free-text reason box (no dropdowns/radios)", () => {
    setup();
    expect(screen.getByText("Early Clock Out")).toBeInTheDocument();
    expect(
      screen.getByText(/have not completed today's required working hours/i)
    ).toBeInTheDocument();
    // Free-text only.
    expect(screen.getByRole("textbox")).toBeInTheDocument();
    expect(document.querySelector("select")).toBeNull();
    expect(document.querySelector('input[type="radio"]')).toBeNull();
  });

  it("enforces the maximum length on the textarea", () => {
    setup();
    expect(screen.getByRole("textbox")).toHaveAttribute(
      "maxlength",
      String(EARLY_REASON_MAX)
    );
  });

  it("disables Clock Out until the trimmed reason meets the minimum length", () => {
    // Below minimum → disabled.
    const short = "a".repeat(EARLY_REASON_MIN - 1);
    const { rerender } = setup({ reason: short });
    const btn = () => screen.getByRole("button", { name: /clock out/i });
    expect(btn()).toBeDisabled();

    // Whitespace padding does not count (trimmed).
    rerender(
      <EarlyClockOutModal
        reason={"   " + "a".repeat(EARLY_REASON_MIN - 2) + "   "}
        onReasonChange={() => {}}
        onCancel={() => {}}
        onSubmit={() => {}}
      />
    );
    expect(btn()).toBeDisabled();

    // At the minimum → enabled.
    rerender(
      <EarlyClockOutModal
        reason={"a".repeat(EARLY_REASON_MIN)}
        onReasonChange={() => {}}
        onCancel={() => {}}
        onSubmit={() => {}}
      />
    );
    expect(btn()).not.toBeDisabled();
  });

  it("fires onSubmit when a valid reason is entered and Clock Out is clicked", () => {
    const { onSubmit } = setup({ reason: "Leaving early for a medical appointment." });
    fireEvent.click(screen.getByRole("button", { name: /clock out/i }));
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("fires onCancel when Cancel is clicked", () => {
    const { onCancel } = setup({ reason: "" });
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("propagates typing through onReasonChange", () => {
    const { onReasonChange } = setup();
    fireEvent.change(screen.getByRole("textbox"), {
      target: { value: "internet outage at home" },
    });
    expect(onReasonChange).toHaveBeenCalledWith("internet outage at home");
  });

  it("renders a server/validation error when provided", () => {
    setup({ reason: "short", error: "Reason must be at least 15 characters." });
    expect(screen.getByRole("alert")).toHaveTextContent(/at least 15 characters/i);
  });

  it("disables both buttons while submitting", () => {
    setup({ reason: "a".repeat(EARLY_REASON_MIN), submitting: true });
    expect(screen.getByRole("button", { name: /cancel/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /clocking out/i })).toBeDisabled();
  });
});
