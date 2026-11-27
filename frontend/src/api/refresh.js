/**
 * Silent access-token refresh — the heart of "stay logged in for 30 days".
 *
 * The long-lived refresh token NEVER lives in JS:
 *   • Web     → it's an HttpOnly cookie the browser attaches automatically to
 *               POST /auth/refresh (we just need withCredentials).
 *   • Desktop → it lives in Electron's OS-encrypted safeStorage; the renderer
 *               asks the main process to perform the refresh over secure IPC.
 *
 * `refreshAccessToken()` is single-flight: if a dozen requests 401 at once,
 * they all await the SAME refresh round-trip instead of stampeding the server
 * (which, with single-use rotation, would trip reuse-detection and kill the
 * session). It returns the new access token, or null if the session is truly
 * over.
 */
import axios from "axios";
import { writeToken, isRemembered } from "./authStorage";

const BASE_URL = import.meta.env.VITE_API_URL || "";

/**
 * Fired on `window` after a silent refresh persists a NEW access token.
 * AuthContext listens for it so its `token` state (and therefore every effect
 * keyed on it) tracks the rotation instead of holding the expired string.
 */
export const TOKEN_REFRESHED_EVENT = "zt:token-refreshed";

// Raw axios call (no interceptors) so a 401 here can't recurse into itself.
const rawRefresh = () =>
  axios.post(
    `${BASE_URL}/auth/refresh`,
    {},
    { withCredentials: true, timeout: 20000 },
  );

const isDesktop = () =>
  typeof window !== "undefined" && window.isDesktopApp === true;

// Returns one of:
//   • a token string → refresh succeeded
//   • null           → TRANSIENT failure (network / 5xx / 503 "Database
//                      temporarily unavailable"). The session is NOT over —
//                      the caller must keep the user logged in and retry later.
//   • false          → the session is DEFINITIVELY over (401/403 from
//                      /auth/refresh). The caller should log out.
async function performRefresh() {
  // Desktop: the main process holds the refresh token and does the HTTP call.
  // It also owns the desktop logout (it reloads to /login on a hard 401), so
  // from the renderer's side we only distinguish "got a token" from "didn't".
  // A missing token is treated as transient here so the renderer doesn't ALSO
  // clear storage / redirect and race the main process.
  if (isDesktop() && window.electronAuth?.refreshToken) {
    try {
      const r = await window.electronAuth.refreshToken();
      return r?.access_token || null;
    } catch {
      return null;
    }
  }
  // Web: the HttpOnly refresh cookie rides along automatically.
  try {
    const res = await rawRefresh();
    return res?.data?.access_token || null;
  } catch (e) {
    // Only a 401/403 from /auth/refresh means the session is truly over. A
    // network error or a 5xx (e.g. 503 "Database temporarily unavailable") is
    // transient — keep the session and let the caller retry.
    const status = e?.response?.status;
    if (status === 401 || status === 403) return false;
    return null;
  }
}

let inFlight = null;

export function refreshAccessToken() {
  if (!inFlight) {
    inFlight = performRefresh()
      .then((token) => {
        if (token) {
          // Persist into whichever tier the "remember" choice selected so the
          // axios request interceptor + route guards pick it up immediately.
          writeToken(token, { remember: isRemembered() });
          // Announce the rotation. Storage writes are invisible to React, and
          // the `storage` event only fires in OTHER tabs — so without this,
          // AuthContext keeps serving the OLD token string and any effect keyed
          // on it never re-runs after a silent refresh.
          if (typeof window !== "undefined") {
            try {
              window.dispatchEvent(new CustomEvent(TOKEN_REFRESHED_EVENT, { detail: { token } }));
            } catch { /* very old browsers without CustomEvent — nothing depends on it */ }
          }
        }
        return token;
      })
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
}
