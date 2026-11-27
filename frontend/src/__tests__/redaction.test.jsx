/**
 * RBAC redaction — frontend invariant.
 *
 * The backend substitutes "***redacted***" for any sensitive column the
 * caller is not entitled to view. The frontend must NEVER render that
 * marker as a value; it must render a neutral "Withheld" pill instead.
 * Hiding the field silently is also wrong — the spec mandates an audit-
 * friendly UX, so we surface that something is there but withheld.
 */
import React from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";

import { RedactedField } from "../components/offboarding/primitives";
import { isRedacted, REDACTED_MARKER } from "../api/offboarding";

describe("RBAC redaction", () => {
  it("treats the canonical marker as redacted", () => {
    expect(isRedacted(REDACTED_MARKER)).toBe(true);
    expect(isRedacted("anything else")).toBe(false);
    expect(isRedacted(null)).toBe(false);
  });

  it("renders a Withheld pill when value is the redacted marker", () => {
    render(<RedactedField value={REDACTED_MARKER} />);
    expect(screen.getByText(/withheld/i)).toBeInTheDocument();
    // The redaction marker MUST NEVER appear as visible text.
    expect(screen.queryByText(REDACTED_MARKER)).toBeNull();
    // Accessible label tells screen readers what's going on.
    expect(screen.getByRole("note")).toHaveAttribute(
      "aria-label",
      expect.stringMatching(/withheld by role-based access control/i),
    );
  });

  it("renders the raw value when not redacted", () => {
    render(<RedactedField value="role eliminated" />);
    expect(screen.getByText("role eliminated")).toBeInTheDocument();
  });

  it("falls back when value is empty", () => {
    render(<RedactedField value={null} fallback="—" />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
