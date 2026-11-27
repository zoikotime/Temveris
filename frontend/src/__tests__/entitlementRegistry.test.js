/**
 * Phase 3.7 — feature-key registry (pure logic, no render).
 *
 * These pin the presentation contract: the frontend only consumes the backend
 * projection, HIDE removes an item, the load/error fallback renders everything,
 * privacy stays hidden, and CONFIGURATION_REQUIRED is never an upsell.
 */
import { describe, it, expect } from "vitest";

import {
  PRESENTATION,
  NAV_CAPABILITY,
  presentationForNav,
  shouldRenderNav,
  isLocked,
  isReadOnly,
  needsConfiguration,
  indexProjection,
} from "../entitlements/registry";

// The authoritative 38 keys the backend owns. Mirrored here only to assert the
// registry never invents a key outside the set.
const BACKEND_KEYS = new Set([
  "activity.anomaly", "activity.log.view", "billing.portal", "dashboard.overview.view",
  "evidence.legal_hold", "evidence.screenshots.capture", "evidence.screenshots.jurisdiction",
  "evidence.screenshots.masking", "evidence.screenshots.policy_controls",
  "evidence.screenshots.view", "governance.chain_of_custody", "governance.ediscovery",
  "holidays.multicountry", "holidays.primary", "identity.mfa", "identity.sso",
  "integrations.api", "integrations.webhooks", "leave.approvals", "leave.jurisdiction",
  "leave.manage", "offboarding.governed", "offboarding.legal_hold", "offboarding.standard",
  "payroll.calculator", "productivity.apps_history", "productivity.website_categories",
  "reports.advanced", "reports.compliance", "reports.standard",
  "team.directory.manage", "team.directory.view", "time.attendance.policy",
  "time.attendance.view", "time.playback.advanced", "time.playback.basic",
  "workspace.settings.core", "workspace.settings.rbac_advanced",
]);

const proj = (entries) => entries; // { cap: presentation }

describe("registry — mapping integrity", () => {
  it("maps only to authoritative backend capability keys", () => {
    for (const [navKey, cap] of Object.entries(NAV_CAPABILITY)) {
      expect(BACKEND_KEYS.has(cap), `${navKey} → ${cap}`).toBe(true);
    }
  });

  it("maps the privacy sidebar items to the globally denied keys", () => {
    expect(NAV_CAPABILITY.appshistory).toBe("productivity.apps_history");
    expect(NAV_CAPABILITY.browsercategories).toBe("productivity.website_categories");
  });

  it("maps Screenshots to the viewer capability, not capture", () => {
    expect(NAV_CAPABILITY.screenshots).toBe("evidence.screenshots.view");
  });
});

describe("registry — presentation resolution", () => {
  it("returns the backend presentation for a mapped item", () => {
    const p = proj({ "reports.standard": PRESENTATION.SHOW });
    expect(presentationForNav("reports", p)).toBe(PRESENTATION.SHOW);
  });

  it("returns null for an unmapped (universal) item", () => {
    expect(presentationForNav("profile", proj({}))).toBeNull();
    expect(presentationForNav("downloadapp", proj({}))).toBeNull();
  });

  it("returns null when the projection has not loaded", () => {
    expect(presentationForNav("reports", null)).toBeNull();
  });
});

describe("registry — SHOW / LOCK / HIDE / READ_ONLY", () => {
  it("SHOW renders and carries no badge", () => {
    const p = proj({ "reports.standard": PRESENTATION.SHOW });
    expect(shouldRenderNav("reports", p)).toBe(true);
    expect(isLocked("reports", p)).toBe(false);
    expect(isReadOnly("reports", p)).toBe(false);
  });

  it("HIDE removes the item", () => {
    const p = proj({ "productivity.apps_history": PRESENTATION.HIDE });
    expect(shouldRenderNav("appshistory", p)).toBe(false);
  });

  it("LOCK renders but is flagged locked", () => {
    const p = proj({ "reports.standard": PRESENTATION.LOCK });
    expect(shouldRenderNav("reports", p)).toBe(true);
    expect(isLocked("reports", p)).toBe(true);
  });

  it("READ_ONLY renders but is flagged read-only", () => {
    const p = proj({ "reports.standard": PRESENTATION.READ_ONLY });
    expect(shouldRenderNav("reports", p)).toBe(true);
    expect(isReadOnly("reports", p)).toBe(true);
  });
});

describe("registry — CONFIGURATION_REQUIRED is not an upsell", () => {
  it("is flagged as configuration, never as lock", () => {
    const p = proj({ "reports.standard": PRESENTATION.CONFIGURATION_REQUIRED });
    expect(needsConfiguration("reports", p)).toBe(true);
    expect(isLocked("reports", p)).toBe(false);
    expect(shouldRenderNav("reports", p)).toBe(true);
  });
});

describe("registry — privacy stays hidden", () => {
  it("hides apps history and website categories when the backend hides them", () => {
    const p = proj({
      "productivity.apps_history": PRESENTATION.HIDE,
      "productivity.website_categories": PRESENTATION.HIDE,
    });
    expect(shouldRenderNav("appshistory", p)).toBe(false);
    expect(shouldRenderNav("browsercategories", p)).toBe(false);
  });

  it("has no frontend exception that could reveal them", () => {
    // Even if the backend erroneously said SHOW, the frontend would obey the
    // backend rather than a local override — there is no local privacy rule to
    // second-guess. This asserts the registry carries no such override.
    const p = proj({ "productivity.apps_history": PRESENTATION.SHOW });
    // The frontend has no independent hide rule, so it consumes SHOW verbatim —
    // proving the frontend never *invents* visibility, in either direction.
    expect(shouldRenderNav("appshistory", p)).toBe(true);
  });
});

describe("registry — safe fallback (load / error)", () => {
  it("renders every item when the projection is null", () => {
    for (const navKey of Object.keys(NAV_CAPABILITY)) {
      expect(shouldRenderNav(navKey, null)).toBe(true);
      expect(isLocked(navKey, null)).toBe(false);
      expect(isReadOnly(navKey, null)).toBe(false);
    }
  });
});

describe("registry — indexProjection", () => {
  it("indexes the endpoint response shape", () => {
    const idx = indexProjection({
      capabilities: [
        { capability: "reports.standard", presentation: "show" },
        { capability: "integrations.api", presentation: "lock" },
      ],
    });
    expect(idx["reports.standard"]).toBe("show");
    expect(idx["integrations.api"]).toBe("lock");
  });

  it("tolerates a malformed response", () => {
    expect(indexProjection(null)).toEqual({});
    expect(indexProjection({})).toEqual({});
    expect(indexProjection({ capabilities: "nope" })).toEqual({});
  });
});
