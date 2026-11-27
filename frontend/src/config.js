// Central API config — works in both dev (proxied via Vite) and Docker (same-origin)
export const API_BASE = import.meta.env.VITE_API_URL || "";

/**
 * Attendance ACTIONS (Clock In / Clock Out / Take a Break / Resume Work) are
 * available in the Temveris Desktop App only — this flag turns them off for the
 * WEBSITE, for every role and every organization, existing and future.
 *
 * The website stays fully read-only for attendance: working status, worked
 * hours, active time, break time, activity, screenshots, timesheets, reports,
 * schedules and leave all continue to display exactly as before.
 *
 * This hides the controls; the server is the authority (WEB_ATTENDANCE_DISABLED
 * in backend/app/attendance_desktop_policy.py rejects the calls themselves).
 * The Electron dashboard does NOT read this flag — the desktop app is untouched.
 *
 * Set back to true (with the backend constant) to restore website attendance.
 * Nothing is deleted; the implementation stays in place.
 */
export const WEB_ATTENDANCE_ACTIONS_ENABLED = false;

/**
 * Suffix shown after the workspace name on the activation flow's Workspace
 * details — "acme-workspace.temveris.example".
 *
 * DISPLAY ONLY. The platform does not route by host: there is no per-workspace
 * subdomain, and the real workspace address is FRONTEND_URL + a path (see
 * app/commercial/activation_status.py, which builds `{FRONTEND_URL}/dashboard`).
 * Nothing in the app resolves this value, links to it, or depends on it — it is
 * the visual representation the design asks for, and the slug itself is stored
 * in OrgSettings.slug like any other workspace setting.
 *
 * If per-workspace subdomains are ever built, this becomes the place the real
 * suffix comes from rather than a second constant appearing elsewhere.
 */
export const WORKSPACE_DOMAIN_SUFFIX = (
  import.meta.env.VITE_WORKSPACE_DOMAIN_SUFFIX || "temveris.example"
).replace(/^\.+/, "");

/**
 * Public web origin, for places that need an absolute URL (the Electron shell
 * opening pages on the website, OAuth redirects from `file://`). Placeholder
 * default until the real domain exists — see TEMVERIS_TODO.md.
 */
export const PUBLIC_ORIGIN = (
  import.meta.env.VITE_PUBLIC_ORIGIN || "https://app.temveris.example"
).replace(/\/+$/, "");

/** Desktop installer downloads (placeholder host — see TEMVERIS_TODO.md). */
const DESKTOP_DOWNLOAD_BASE = "https://updates.temveris.example";
export const DESKTOP_DOWNLOADS = {
  win: `${DESKTOP_DOWNLOAD_BASE}/win/Temveris-Setup.exe`,
  mac: `${DESKTOP_DOWNLOAD_BASE}/mac/Temveris.dmg`,
  linux: `${DESKTOP_DOWNLOAD_BASE}/linux/Temveris-linux.deb`,
};

const wsProto = window.location.protocol === "https:" ? "wss:" : "ws:";
export const WS_URL = import.meta.env.VITE_WS_URL || `${wsProto}//${window.location.host}/ws`;

/** Retry a fetch call up to `retries` times on 503 / network errors with exponential backoff */
export const fetchWithRetry = async (url, opts, retries = 2) => {
  for (let i = 0; i <= retries; i++) {
    try {
      const res = await fetch(url, opts);
      if (res.status === 503 && i < retries) {
        await new Promise(r => setTimeout(r, 1000 * (i + 1)));
        continue;
      }
      return res;
    } catch (err) {
      if (i >= retries) throw err;
      await new Promise(r => setTimeout(r, 1000 * (i + 1)));
    }
  }
};
