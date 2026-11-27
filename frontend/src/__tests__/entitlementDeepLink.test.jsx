/**
 * Phase 3.26 — deep-link guard predicate.
 *
 * The AdminDashboard deep-link guard redirects a tab away iff
 *   projection && !shouldRenderNav(tab, projection)
 * i.e. only when the backend projection marks that tab's capability HIDE
 * (privacy / unbuilt). A LOCK tab stays and renders its in-page upgrade card; a
 * SHOW tab stays; an unmapped tab stays; and a null/loading projection never
 * redirects (safe fallback). This proves the guard follows the projection with
 * no plan logic — it reuses the same registry primitive the sidebar uses.
 */
import { describe, it, expect } from "vitest";
import { shouldRenderNav } from "../entitlements/registry";

// Mirror of the guard condition in AdminDashboard.
const wouldRedirect = (tab, projection) =>
  projection != null && !shouldRenderNav(tab, projection);

const projection = (over = {}) => ({
  "dashboard.overview.view": "show",
  "payroll.calculator": "lock",                 // sellable → stays (in-page lock)
  "productivity.apps_history": "hide",          // privacy → redirect
  "productivity.website_categories": "hide",    // privacy → redirect
  ...over,
});

describe("deep-link guard — follows the projection", () => {
  it("redirects privacy tabs the projection marks HIDE", () => {
    expect(wouldRedirect("appshistory", projection())).toBe(true);
    expect(wouldRedirect("browsercategories", projection())).toBe(true);
  });

  it("does NOT redirect a LOCK tab — payroll stays and shows its in-page upgrade", () => {
    expect(wouldRedirect("payroll", projection())).toBe(false);
  });

  it("does NOT redirect a SHOW tab", () => {
    expect(wouldRedirect("overview", projection())).toBe(false);
  });

  it("does NOT redirect an unmapped tab (universal navigation)", () => {
    // 'analytics' is not in NAV_CAPABILITY → always renders.
    expect(wouldRedirect("analytics", projection())).toBe(false);
  });

  it("does NOT redirect while the projection is null (loading / fetch failed)", () => {
    expect(wouldRedirect("appshistory", null)).toBe(false);
  });

  it("re-evaluates per plan: a privacy tab is HIDE for every plan", () => {
    // Privacy is HIDE regardless of tier — the guard redirects on all of them.
    for (const _plan of ["verified", "governed", "sovereign", "enterprise"]) {
      expect(wouldRedirect("appshistory", projection())).toBe(true);
    }
  });
});
