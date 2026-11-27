/**
 * authFetch — raw `fetch` with the SAME session semantics as api/axios.js.
 *
 * Why this exists
 * ---------------
 * Everything that goes through `API` (axios) gets three things for free from
 * the response interceptor in api/axios.js:
 *
 *   1. The token attached from whichever storage tier holds it.
 *   2. On 401 (short-lived access token expired) → ONE single-flight silent
 *      refresh → replay the original request with the new token.
 *   3. A 15 s timeout instead of hanging forever.
 *
 * Screens that call `fetch()` directly get NONE of it. Their 401 is final: the
 * component decides the load "failed" and renders an error state, even though
 * the session is perfectly alive and the very next axios call refreshes the
 * token silently. That is exactly the intermittent "Could not load workspace
 * profile" screen — it fires whenever the settings page happens to be opened
 * with an already-expired access token.
 *
 * `authFetch` closes that gap for the raw-fetch call sites without rewriting
 * them onto axios: same Response object back, same `.ok` / `.json()` usage,
 * plus refresh-and-replay, a timeout, and a retry on transient failures.
 *
 *   const res = await authFetch(`${API_BASE}/org/settings`);
 *   if (res.ok) setData(await res.json());
 *
 * Reuses refreshAccessToken() from api/refresh.js, so a burst of 401s across
 * axios AND authFetch still share ONE refresh round-trip (single-use refresh
 * rotation would otherwise trip reuse-detection and kill the session).
 */
import { readToken, redirectToLogin } from "./authStorage";
import { refreshAccessToken } from "./refresh";

// Matches the axios client's `timeout: 15000`. The previous per-call 8 s
// budget was tighter than the rest of the app, so a cold container or a slow
// mobile connection produced a hard error screen on a request axios would
// still have been patiently waiting on.
const DEFAULT_TIMEOUT = 15000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Re-read the token on EVERY attempt so a replay picks up the rotated one. */
const withAuth = (init) => {
  const headers = new Headers(init.headers || {});
  const token = readToken();          // checks localStorage AND sessionStorage
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (typeof window !== "undefined" && window.isDesktopApp === true) {
    headers.set("X-Client-Type", "desktop");
  }
  return { ...init, headers };
};

/**
 * @param {string} url
 * @param {object} [opts]  standard fetch init, plus:
 *   timeout {number}  ms before the request is aborted (default 15000)
 *   retries {number}  extra attempts for TRANSIENT failures — network error,
 *                     timeout, or 5xx (default 1). A 401 replay is counted
 *                     separately and is always allowed exactly once.
 * @returns {Promise<Response>}
 */
export async function authFetch(url, { timeout = DEFAULT_TIMEOUT, retries = 1, ...init } = {}) {
  let attempt = 0;
  let didRefresh = false;

  for (;;) {
    let res;
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeout);
      try {
        res = await fetch(url, { ...withAuth(init), signal: ctrl.signal });
      } finally {
        clearTimeout(timer);
      }
    } catch (err) {
      // Aborted by our timeout, or the network dropped. Retry once before
      // giving up — a single blip should not become a permanent error screen.
      if (attempt < retries) { attempt++; await sleep(300 * attempt); continue; }
      throw err;
    }

    // ── 401 → silent refresh → replay once ───────────────────────────────────
    // Mirrors the axios interceptor's contract with refreshAccessToken():
    //   string → new access token, replay the request
    //   false  → session is definitively over, log out
    //   null   → transient (network / 5xx / DB unavailable): session is NOT
    //            over, so hand the 401 back and let the caller retry later.
    if (res.status === 401 && !didRefresh) {
      didRefresh = true;
      let result = null;
      try { result = await refreshAccessToken(); } catch { result = false; }
      if (typeof result === "string" && result) continue;   // replay with fresh token
      if (result === false) redirectToLogin();              // shared, self-deduping logout
      return res;
    }

    // Server-side wobble (cold start, 502 from the proxy, 503 while the DB
    // pool warms up) — one more attempt costs little and saves the screen.
    if (res.status >= 500 && attempt < retries) {
      attempt++;
      await sleep(300 * attempt);
      continue;
    }

    return res;
  }
}

/**
 * Convenience wrapper for read-only endpoints whose callers only care about
 * "did I get data": resolves to the parsed body, or null on any failure.
 * Never throws, so it is safe to fire several in parallel without a catch.
 */
export async function authFetchJson(url, opts) {
  try {
    const res = await authFetch(url, opts);
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}

export default authFetch;
