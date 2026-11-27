/**
 * Tenant-isolation + auth-isolation tests (§8 frontend security).
 *
 * Two invariants the UI must uphold:
 *   1. The admin API NEVER sends a client-supplied tenant id — tenant scope is
 *      derived server-side from the JWT. A spoofable client tenant param would
 *      be a cross-tenant hole. We spy on the shared axios instance and assert
 *      no `tenant`/`tenant_id`/`organization_id` ever leaves in params.
 *   2. The Former Worker Portal uses a SEPARATE auth universe: its own token
 *      key in sessionStorage (never localStorage, never the active-worker key),
 *      cleared on logout. A leaked admin token must not authenticate the portal
 *      and vice-versa.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

// Mock the shared admin axios so we can inspect every call's params.
// `vi.hoisted` makes these mock fns available both inside the (hoisted)
// vi.mock factory and in the test bodies — referencing a plain top-level
// const from the factory would throw "Cannot access before initialization".
const { get, post, del, patch } = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), del: vi.fn(), patch: vi.fn(),
}));
vi.mock("../api/axios", () => ({ default: { get, post, delete: del, patch } }));

import * as offb from "../api/offboarding";

const TENANT_KEYS = ["tenant", "tenant_id", "organization_id", "org_id"];

function assertNoTenantLeak(mockFn) {
  for (const call of mockFn.mock.calls) {
    const cfg = call[1] || {};
    const params = cfg.params || {};
    const body = call[1] && !cfg.params ? call[1] : {};
    for (const k of TENANT_KEYS) {
      expect(params[k], `param '${k}' must not be client-supplied`).toBeUndefined();
      expect(body[k], `body '${k}' must not be client-supplied`).toBeUndefined();
    }
  }
}

describe("admin API tenant isolation", () => {
  beforeEach(() => {
    get.mockReset().mockResolvedValue({ data: { items: [], total: 0 } });
    post.mockReset().mockResolvedValue({ data: {} });
    del.mockReset().mockResolvedValue({ data: {} });
  });

  it("listCases sends only the documented filter params — no tenant id", async () => {
    await offb.listCases({ status: "in_progress", limit: 25, offset: 0 });
    expect(get).toHaveBeenCalledWith("/api/offboarding/cases", {
      params: { status: "in_progress", limit: 25, offset: 0 },
    });
    assertNoTenantLeak(get);
  });

  it("security + governance reads never carry a tenant id", async () => {
    await offb.listReconciliation({ limit: 100 });
    await offb.listSecurityEvents({ severity: "critical" });
    await offb.getCertificate("case_1");
    await offb.listEvidencePackages("case_1");
    assertNoTenantLeak(get);
  });

  it("revoke-access posts only the explicit target — no tenant id", async () => {
    await offb.revokeAccess({ worker_id: 7, reason: "security" });
    expect(post).toHaveBeenCalledWith("/api/offboarding/revoke-access", { worker_id: 7, reason: "security" });
    for (const call of post.mock.calls) {
      const body = call[1] || {};
      for (const k of TENANT_KEYS) expect(body[k]).toBeUndefined();
    }
  });
});

describe("Former Worker Portal auth isolation", () => {
  beforeEach(() => {
    sessionStorage.clear();
    try { localStorage.clear(); } catch { /* ignore */ }
    vi.resetModules();
  });

  it("stores its session token in sessionStorage under a portal-only key, not localStorage", async () => {
    const portal = await import("../api/formerWorkerPortal");
    portal.writePortalToken("pst_secret123");
    expect(portal.readPortalToken()).toBe("pst_secret123");

    // Must live under a portal-scoped sessionStorage key…
    const sessionKeys = Object.keys(sessionStorage);
    expect(sessionKeys.some((k) => /fw|portal/i.test(k))).toBe(true);
    // …and NOT in localStorage (30-min session must not outlive the tab).
    expect(Object.keys(localStorage).some((k) => /fw|portal/i.test(k))).toBe(false);
  });

  it("clears the portal token on logout", async () => {
    const portal = await import("../api/formerWorkerPortal");
    portal.writePortalToken("pst_secret123");
    portal.clearPortalToken();
    expect(portal.readPortalToken()).toBe("");
  });

  it("does NOT reuse the active-worker token storage (separate universe)", async () => {
    // Simulate an active-worker token sitting in localStorage.
    try { localStorage.setItem("zt_access_token", "admin.jwt.token"); } catch { /* ignore */ }
    const portal = await import("../api/formerWorkerPortal");
    // The portal reader must ignore the admin token entirely.
    expect(portal.readPortalToken()).toBe("");
  });
});
