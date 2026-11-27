import axios from "axios";
import { readToken, redirectToLogin } from "./authStorage";
import { refreshAccessToken } from "./refresh";
import { announceBillingBlocked } from "./billingBlocked";

const API = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "",
  timeout: 15000, // hard cap so a stalled request never freezes the UI
  // Send + receive the HttpOnly refresh cookie on auth calls. Harmless on
  // every other endpoint (the cookie is scoped to /auth on the server).
  withCredentials: true,
});

// ─── Auth header injection ──────────────────────────────────────────────────
// readToken() looks at BOTH localStorage and sessionStorage so the "Remember
// this device" choice on the login page picks where the token lives:
//   • remember=true  → localStorage (survives browser close)
//   • remember=false → sessionStorage (cleared on last tab close)
API.interceptors.request.use((config) => {
  const token = readToken();
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  // Tell the backend this is the Electron shell so it returns the refresh
  // token in the body (for safeStorage) instead of setting a cookie.
  if (typeof window !== "undefined" && window.isDesktopApp === true) {
    config.headers["X-Client-Type"] = "desktop";
  }
  return config;
});

// ─── In-flight GET deduplication ────────────────────────────────────────────
// If two components ask for the same URL within a few hundred ms, share the
// same network request instead of hitting the backend twice.
const inflight = new Map();

API.interceptors.request.use((config) => {
  if ((config.method || "get").toLowerCase() !== "get" || config.skipDedup) {
    return config;
  }
  const key = `${config.baseURL || ""}${config.url}|${JSON.stringify(config.params || {})}`;
  config.__dedupKey = key;
  return config;
});

const _origRequest = API.request.bind(API);
API.request = (config) => {
  const method = (config.method || "get").toLowerCase();
  if (method !== "get" || config.skipDedup) return _origRequest(config);

  const key = `${config.baseURL || ""}${config.url}|${JSON.stringify(config.params || {})}`;
  if (inflight.has(key)) return inflight.get(key);

  const promise = _origRequest(config).finally(() => {
    // Keep the entry for one tick so synchronous duplicates also fold in,
    // but drop it quickly so a refetch still hits the network.
    setTimeout(() => inflight.delete(key), 50);
  });
  inflight.set(key, promise);
  return promise;
};

// ─── 401 → silent refresh → retry, else hard logout ─────────────────────────
// When a request 401s because the short-lived access token expired, we try ONE
// silent refresh (single-flight, shared across concurrent 401s) and replay the
// original request with the new token. Only if the refresh itself fails do we
// clear storage and bounce to the login screen — that's what makes a 30-day
// session feel like "never logged out".
const _AUTH_NOREFRESH = ["/auth/refresh", "/auth/login", "/auth/login-json"];

API.interceptors.response.use(
  (res) => res,
  async (err) => {
    const response = err?.response;
    const config = err?.config;

    if (response?.status === 401 && config && !config.__isRetry) {
      const url = config.url || "";
      const refreshable = !_AUTH_NOREFRESH.some((p) => url.includes(p));

      if (refreshable) {
        try {
          const result = await refreshAccessToken();
          if (typeof result === "string" && result) {
            config.__isRetry = true;
            // The request interceptor will re-attach the (now fresh) token,
            // but set it explicitly too so the replay is unambiguous.
            config.headers = config.headers || {};
            config.headers.Authorization = `Bearer ${result}`;
            return API(config);
          }
          if (result === null) {
            // TRANSIENT failure (network / 5xx / 503 "Database temporarily
            // unavailable"). The session is NOT over — keep the user logged in,
            // surface the original error, and let the next request retry. Do
            // NOT clear storage or redirect: on desktop, clearing storage would
            // also trip the main process's "localStorage cleared → logout".
            return Promise.reject(err);
          }
          // result === false → session is definitively over → fall through.
        } catch { /* fall through to logout */ }
      }

      // Session is genuinely over — send the user to login via the single shared
      // pipeline. It self-dedupes (beginLogout), clears storage + navigates on
      // web, and hands off to the Electron main process on desktop (which owns
      // nav + storage) so we never race it into a white-screen reload.
      redirectToLogin();
    }

    // ─── 403 organization suspended → end the session with an explanation ────
    // The backend gates every authenticated request on the tenant's lifecycle
    // state (app/org/lifecycle.py), so this fires the moment a super admin
    // suspends the organization — the user does not have to wait for their
    // token to expire.
    //
    // Routed through the SAME logout pipeline as a dead session rather than a
    // dedicated /organization-suspended route: that pipeline is already correct
    // on desktop (hands off to the Electron main process) and cannot produce a
    // redirect loop, because the login page it lands on makes no authenticated
    // calls. The notice is what turns it from an unexplained sign-out into a
    // clear message.
    // ─── 402 payment overdue → explain it, do NOT end the session ───────────
    // Distinct from the 403 below on purpose. A billing-suspended organization is
    // read-only, not locked out: everyone can still sign in, read, and clock in
    // and out, and the billing pages stay writable so the customer can pay.
    // Logging them out would remove the only route to recovery.
    //
    // The promise is still rejected below, so existing error handling is unchanged.
    // This adds the explanation that was previously missing entirely — grep 402 in
    // the frontend returned nothing before this.
    if (response?.status === 402) {
      announceBillingBlocked({
        message: response?.data?.detail || null,
        path: config?.url || null,
      });
    }

    if (response?.status === 403) {
      const code = response?.data?.detail?.code;
      if (code === "organization_suspended" || code === "organization_not_found") {
        redirectToLogin({
          notice:
            response?.data?.detail?.message ||
            "Your organization is currently suspended. Please contact your administrator.",
        });
      }
    }
    return Promise.reject(err);
  }
);

// ─── Human-readable message for save / mutation failures ────────────────────
// A failed save fails two very different ways, and both the UX and any bug
// report should tell them apart:
//   • No `err.response` → the request got no reply: server unreachable, CORS,
//     or the 15s timeout above fired. An environment/network problem, not bad
//     input — the old "Couldn't save" message hid this entirely.
//   • `err.response` present → the server answered; surface its `detail`, or a
//     status-tagged fallback so the HTTP status lands in the message (and the
//     console) instead of a generic string.
export function saveErrorMessage(err, fallback = "Couldn't save — please try again") {
  const res = err?.response;
  if (!res) {
    const timedOut = err?.code === "ECONNABORTED";
    console.error("[save] no response from server", { code: err?.code, message: err?.message });
    return timedOut
      ? "The server took too long to respond — check your connection and try again."
      : "Can't reach the server right now — check your connection and try again.";
  }
  console.error("[save] server responded with error", { status: res.status, detail: res.data?.detail });
  const detail = res.data?.detail;
  if (typeof detail === "string" && detail) return detail;
  return `${fallback} (server error ${res.status})`;
}

export default API;
