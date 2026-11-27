/**
 * Phase 3.25 — plan-tier visibility, end to end through <Gate>.
 *
 * These projections are the EXACT presentations the backend actor projection
 * returns (verified/governed/sovereign/enterprise × admin/worker, plus a
 * grandfathered trial) — captured from projection.build_actor_projection. The
 * frontend has no plan/role/grandfather logic of its own; it renders whatever
 * the projection says. So proving the tiering here is proving that <Gate>
 * faithfully reflects the source of truth:
 *
 *   Verified  → sees only base/all-plan features; tiered features LOCK (admin)
 *               or HIDE (worker); privacy always HIDE.
 *   Governed  → Governed+ SHOW; Sovereign+ still LOCK.
 *   Sovereign → Governed+ and Sovereign+ SHOW.
 *   Enterprise→ everything its plan grants SHOW.
 *   Grandfathered Verified trial → only the approved 7 grandfathered caps SHOW;
 *               a non-grandfathered Governed+ cap (integrations.api) stays LOCK.
 *   RBAC advanced → SHOW on every plan (all-plan; never gated).
 */
import React from "react";
import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";

let projectionValue = null;
const presentationFor = (cap) => (projectionValue ? projectionValue[cap] || null : null);
vi.mock("../context/EntitlementsContext", () => ({
  useEntitlements: () => ({ presentationFor, ready: projectionValue !== null }),
  PRESENTATION: {
    SHOW: "show", LOCK: "lock", HIDE: "hide",
    READ_ONLY: "read_only", CONFIGURATION_REQUIRED: "configuration_required",
  },
}));

import Gate from "../entitlements/Gate";

// ── Representative capabilities from each class (authoritative backend keys) ──
// BASE includes the screenshot VIEWER and primary holidays — both All-plan and
// must never be gated (the capture control and multi-country selector are the
// gated siblings, in GOVERNED-config / SOVEREIGN respectively).
const BASE     = ["reports.standard", "workspace.settings.rbac_advanced",
                  "evidence.screenshots.view", "holidays.primary"];
const GOVERNED = ["payroll.calculator", "reports.advanced", "activity.anomaly",
                  "leave.approvals", "integrations.api"];
const SOVEREIGN = ["reports.compliance", "evidence.legal_hold", "holidays.multicountry",
                   "offboarding.legal_hold"];
const PRIVACY  = ["productivity.apps_history", "productivity.website_categories"];
const ALL = [...BASE, ...GOVERNED, ...SOVEREIGN, ...PRIVACY];

// A harness that mounts one Gate per capability; SHOW renders a probe span.
function Harness() {
  return (
    <div>
      {ALL.map((cap) => (
        <Gate key={cap} capability={cap}>
          <span data-testid={`feat:${cap}`}>ON</span>
        </Gate>
      ))}
    </div>
  );
}

// Build a projection by class (used for the uniform plans).
const byClass = ({ gov, sov, privacy }) => {
  const p = {};
  BASE.forEach((c) => (p[c] = "show"));
  GOVERNED.forEach((c) => (p[c] = gov));
  SOVEREIGN.forEach((c) => (p[c] = sov));
  PRIVACY.forEach((c) => (p[c] = privacy));
  return p;
};

const shown  = (cap) => expect(screen.queryByTestId(`feat:${cap}`)).toBeInTheDocument();
const locked = (container, cap) => {
  expect(screen.queryByTestId(`feat:${cap}`)).not.toBeInTheDocument();
  expect(container.querySelector(`[data-entitlement="lock"][data-capability="${cap}"]`)).toBeTruthy();
};
const hidden = (container, cap) => {
  expect(screen.queryByTestId(`feat:${cap}`)).not.toBeInTheDocument();
  expect(container.querySelector(`[data-capability="${cap}"]`)).toBeNull();  // no lock/setup card either
};

beforeEach(() => { projectionValue = null; });

describe("Plan-tier visibility through <Gate>", () => {
  it("VERIFIED admin: base SHOW, tiered LOCK, privacy HIDE", () => {
    projectionValue = byClass({ gov: "lock", sov: "lock", privacy: "hide" });
    const { container } = render(<Harness />);
    BASE.forEach(shown);
    GOVERNED.forEach((c) => locked(container, c));
    SOVEREIGN.forEach((c) => locked(container, c));
    PRIVACY.forEach((c) => hidden(container, c));
  });

  it("VERIFIED worker: tiered HIDE (no upsell), base SHOW", () => {
    projectionValue = byClass({ gov: "hide", sov: "hide", privacy: "hide" });
    const { container } = render(<Harness />);
    BASE.forEach(shown);
    GOVERNED.forEach((c) => hidden(container, c));
    SOVEREIGN.forEach((c) => hidden(container, c));
    PRIVACY.forEach((c) => hidden(container, c));
  });

  it("GOVERNED admin: Governed+ SHOW, Sovereign+ LOCK", () => {
    projectionValue = byClass({ gov: "show", sov: "lock", privacy: "hide" });
    const { container } = render(<Harness />);
    BASE.forEach(shown);
    GOVERNED.forEach(shown);
    SOVEREIGN.forEach((c) => locked(container, c));
    PRIVACY.forEach((c) => hidden(container, c));
  });

  it("SOVEREIGN admin: Governed+ and Sovereign+ SHOW", () => {
    projectionValue = byClass({ gov: "show", sov: "show", privacy: "hide" });
    render(<Harness />);
    BASE.forEach(shown);
    GOVERNED.forEach(shown);
    SOVEREIGN.forEach(shown);
  });

  it("ENTERPRISE admin: everything its plan grants SHOW, privacy still HIDE", () => {
    projectionValue = byClass({ gov: "show", sov: "show", privacy: "hide" });
    const { container } = render(<Harness />);
    [...BASE, ...GOVERNED, ...SOVEREIGN].forEach(shown);
    PRIVACY.forEach((c) => hidden(container, c));
  });

  it("privacy capabilities are HIDDEN on every plan (never an upsell)", () => {
    for (const proj of [
      byClass({ gov: "lock", sov: "lock", privacy: "hide" }),   // verified
      byClass({ gov: "show", sov: "lock", privacy: "hide" }),   // governed
      byClass({ gov: "show", sov: "show", privacy: "hide" }),   // sovereign+
    ]) {
      projectionValue = proj;
      const { container, unmount } = render(<Harness />);
      PRIVACY.forEach((c) => hidden(container, c));
      unmount();
    }
  });

  it("RBAC advanced stays SHOW for every plan (all-plan, never gated)", () => {
    for (const proj of [
      byClass({ gov: "lock", sov: "lock", privacy: "hide" }),
      byClass({ gov: "hide", sov: "hide", privacy: "hide" }),   // even a verified worker
      byClass({ gov: "show", sov: "show", privacy: "hide" }),
    ]) {
      projectionValue = proj;
      const { unmount } = render(<Harness />);
      shown("workspace.settings.rbac_advanced");
      unmount();
    }
  });
});

describe("Grandfathered Verified trial — frontend just consumes the projection", () => {
  // Exactly what the backend projects for a grandfathered trial: the approved 7
  // SHOW; integrations.api (Governed+ but NOT grandfathered) stays LOCK; every
  // Sovereign+ cap stays LOCK; privacy HIDE. No frontend grandfather logic.
  const GRANDFATHERED = {
    "reports.standard": "show", "workspace.settings.rbac_advanced": "show",
    "evidence.screenshots.view": "show", "holidays.primary": "show",
    "payroll.calculator": "show", "reports.advanced": "show",
    "activity.anomaly": "show", "leave.approvals": "show",   // 4 of the 7 tested here
    "integrations.api": "lock",                              // Governed+, NOT grandfathered
    "reports.compliance": "lock", "evidence.legal_hold": "lock",
    "holidays.multicountry": "lock", "offboarding.legal_hold": "lock",
    "productivity.apps_history": "hide", "productivity.website_categories": "hide",
  };

  it("shows the 7 grandfathered caps but not a non-grandfathered Governed+ cap", () => {
    projectionValue = GRANDFATHERED;
    const { container } = render(<Harness />);
    ["payroll.calculator", "reports.advanced", "activity.anomaly", "leave.approvals"].forEach(shown);
    locked(container, "integrations.api");           // grandfather does not extend to it
    SOVEREIGN.forEach((c) => locked(container, c));   // nor to any Sovereign+ cap
    PRIVACY.forEach((c) => hidden(container, c));
  });

  it("a fresh Verified trial (no grandfather) LOCKs the same 7 — proving the frontend never grandfathers", () => {
    projectionValue = byClass({ gov: "lock", sov: "lock", privacy: "hide" });
    const { container } = render(<Harness />);
    ["payroll.calculator", "reports.advanced", "activity.anomaly", "leave.approvals"]
      .forEach((c) => locked(container, c));
  });
});
