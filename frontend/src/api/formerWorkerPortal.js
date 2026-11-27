/**
 * Temveris — Former Worker Portal API client.
 *
 * INTENTIONALLY a separate axios instance from the active-worker API. The
 * portal bearer is a one-time session token (NOT the active-worker access
 * JWT), and it must never accidentally ride a request to an admin route.
 * The two universes share no headers, no interceptors, no storage key.
 *
 * Storage:
 *  • sessionStorage only — never localStorage. The portal session expires
 *    in 30 minutes; persisting longer would defeat that floor.
 *  • A single key (FW_PORTAL_TOKEN_KEY) — cleared on logout or on any 401.
 */
import axios from "axios";

const BASE = "/api/offboarding/former-worker";
const FW_PORTAL_TOKEN_KEY = "zt_fw_portal_token";

const portalAPI = axios.create({
  baseURL: import.meta.env.VITE_API_URL || "",
  timeout: 15000,
  // No credentials — the portal does not use cookies. Pure Bearer.
  withCredentials: false,
});

portalAPI.interceptors.request.use((config) => {
  const t = readPortalToken();
  if (t) config.headers.Authorization = `Bearer ${t}`;
  return config;
});

portalAPI.interceptors.response.use(
  (r) => r,
  (err) => {
    // A 401 means the session expired or was revoked. Drop the token so the
    // UI bounces back to /former-worker/login.
    if (err?.response?.status === 401) {
      clearPortalToken();
    }
    return Promise.reject(err);
  }
);

export const readPortalToken  = () => sessionStorage.getItem(FW_PORTAL_TOKEN_KEY) || "";
export const writePortalToken = (t) => sessionStorage.setItem(FW_PORTAL_TOKEN_KEY, t);
export const clearPortalToken = () => sessionStorage.removeItem(FW_PORTAL_TOKEN_KEY);

// ── Auth ──────────────────────────────────────────────────────────────
export const portalLogin = (portalToken) =>
  portalAPI.post(`${BASE}/auth/login`, { portal_token: portalToken }).then((r) => r.data);

export const portalVerifyMfa = (sessionToken, otp) =>
  portalAPI.post(`${BASE}/auth/mfa/verify`, { session_token: sessionToken, otp })
    .then((r) => {
      // The verify response confirms MFA. The CALLER (login screen) is what
      // stores the session token in sessionStorage so the rest of the portal
      // can use it via the Authorization header.
      return r.data;
    });

export const portalLogout = () =>
  portalAPI.post(`${BASE}/auth/logout`).finally(clearPortalToken).then(() => undefined);

export const portalEnrollMfa = (portalToken, alternateEmail) =>
  portalAPI.post(`${BASE}/auth/mfa/enroll`, {
    portal_token: portalToken,
    alternate_email: alternateEmail,
  }).then((r) => r.data);

export const portalEnrollMfaVerify = (portalToken, otp) =>
  portalAPI.post(`${BASE}/auth/mfa/enroll/verify`, {
    portal_token: portalToken,
    otp,
  }).then((r) => r.data);

// ── Profile + documents + tickets ─────────────────────────────────────
export const portalMe          = () => portalAPI.get(`${BASE}/me`).then((r) => r.data);
export const portalListDocs    = () => portalAPI.get(`${BASE}/documents`).then((r) => r.data);
export const portalDownload    = (scope, docRef) =>
  portalAPI.post(`${BASE}/documents/download`, { scope, document_ref: docRef }).then((r) => r.data);
export const portalSupport     = (body) => portalAPI.post(`${BASE}/support-requests`, body).then((r) => r.data);
export const portalDsar        = (body) => portalAPI.post(`${BASE}/data-rights-requests`, body).then((r) => r.data);
export const portalAccessLog   = (limit = 50) => portalAPI.get(`${BASE}/access-log`, { params: { limit } }).then((r) => r.data);
