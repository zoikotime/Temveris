/**
 * Phase 3.25 — the reusable <Gate> / useCapability primitive.
 *
 * Proves the five presentation states render correctly and that a null
 * projection behaves like SHOW (safe fallback). The projection is mocked so the
 * test isolates the Gate's decision logic — there is no plan/role logic in the
 * frontend to test, only the presentation → render mapping.
 */
import React from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Controllable projection, injected through a mocked EntitlementsContext.
let projectionValue = null;
const presentationFor = (cap) => (projectionValue ? projectionValue[cap] || null : null);
vi.mock("../context/EntitlementsContext", () => ({
  useEntitlements: () => ({ presentationFor, ready: projectionValue !== null }),
  PRESENTATION: {
    SHOW: "show", LOCK: "lock", HIDE: "hide",
    READ_ONLY: "read_only", CONFIGURATION_REQUIRED: "configuration_required",
  },
}));

import Gate, { useCapability } from "../entitlements/Gate";

beforeEach(() => { projectionValue = null; });

const Feature = () => <div>FEATURE BODY</div>;

describe("<Gate> — presentation → render", () => {
  it("SHOW renders the feature", () => {
    projectionValue = { "reports.advanced": "show" };
    render(<Gate capability="reports.advanced"><Feature /></Gate>);
    expect(screen.getByText("FEATURE BODY")).toBeInTheDocument();
  });

  it("LOCK renders the upgrade card, not the feature", () => {
    projectionValue = { "reports.advanced": "lock" };
    render(<Gate capability="reports.advanced"><Feature /></Gate>);
    expect(screen.queryByText("FEATURE BODY")).not.toBeInTheDocument();
    expect(screen.getByText(/not included in your plan/i)).toBeInTheDocument();
  });

  it("HIDE renders nothing at all", () => {
    projectionValue = { "productivity.apps_history": "hide" };
    const { container } = render(
      <Gate capability="productivity.apps_history"><Feature /></Gate>,
    );
    expect(screen.queryByText("FEATURE BODY")).not.toBeInTheDocument();
    expect(container.textContent).toBe("");
  });

  it("CONFIGURATION_REQUIRED renders a setup CTA, never an upsell", () => {
    projectionValue = { "evidence.screenshots.capture": "configuration_required" };
    render(<Gate capability="evidence.screenshots.capture"><Feature /></Gate>);
    expect(screen.queryByText("FEATURE BODY")).not.toBeInTheDocument();
    expect(screen.getByText(/setup required/i)).toBeInTheDocument();
    expect(screen.queryByText(/higher plan/i)).not.toBeInTheDocument();
  });

  it("READ_ONLY renders the feature and exposes readOnly to children", () => {
    projectionValue = { "leave.approvals": "read_only" };
    render(
      <Gate capability="leave.approvals">
        {({ readOnly }) => <button disabled={readOnly}>Approve</button>}
      </Gate>,
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("null projection (unresolved / fetch failed) behaves like SHOW", () => {
    projectionValue = null;
    render(<Gate capability="reports.advanced"><Feature /></Gate>);
    expect(screen.getByText("FEATURE BODY")).toBeInTheDocument();
  });

  it("honours a custom lock node", () => {
    projectionValue = { "payroll.calculator": "lock" };
    render(
      <Gate capability="payroll.calculator" lock={<div>CUSTOM LOCK</div>}>
        <Feature />
      </Gate>,
    );
    expect(screen.getByText("CUSTOM LOCK")).toBeInTheDocument();
  });

  it("render-prop form gets full manual control", () => {
    projectionValue = { "activity.anomaly": "lock" };
    render(
      <Gate capability="activity.anomaly" render={(s) => <div>{s.isLock ? "LOCKED" : "OK"}</div>} />,
    );
    expect(screen.getByText("LOCKED")).toBeInTheDocument();
  });
});

describe("useCapability — flags", () => {
  function Probe({ cap }) {
    const s = useCapability(cap);
    return (
      <span data-testid="flags">
        {[s.isShow && "show", s.isLock && "lock", s.isHidden && "hide",
          s.isReadOnly && "ro", s.needsConfig && "cfg",
          s.visible && "visible", s.interactive && "interactive"]
          .filter(Boolean).join(",")}
      </span>
    );
  }

  it("SHOW → interactive + visible", () => {
    projectionValue = { "reports.standard": "show" };
    render(<Probe cap="reports.standard" />);
    expect(screen.getByTestId("flags").textContent).toBe("show,visible,interactive");
  });

  it("LOCK → visible but not interactive", () => {
    projectionValue = { "reports.compliance": "lock" };
    render(<Probe cap="reports.compliance" />);
    expect(screen.getByTestId("flags").textContent).toBe("lock,visible");
  });

  it("HIDE → neither visible nor interactive", () => {
    projectionValue = { "productivity.website_categories": "hide" };
    render(<Probe cap="productivity.website_categories" />);
    expect(screen.getByTestId("flags").textContent).toBe("hide");
  });

  it("READ_ONLY → visible, not interactive, readOnly", () => {
    projectionValue = { "leave.approvals": "read_only" };
    render(<Probe cap="leave.approvals" />);
    expect(screen.getByTestId("flags").textContent).toBe("ro,visible");
  });

  it("null projection → treated as SHOW (safe fallback)", () => {
    projectionValue = null;
    render(<Probe cap="reports.advanced" />);
    expect(screen.getByTestId("flags").textContent).toBe("show,visible,interactive");
  });
});
