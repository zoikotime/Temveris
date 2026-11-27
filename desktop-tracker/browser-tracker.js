"use strict";

/**
 * browser-tracker.js — Native browser tab intelligence (no extension required)
 *
 * Reads the URL bar of Chrome / Edge / Firefox / Brave via the Windows
 * UI Automation API and streams tab sessions to the backend's existing
 * /browser/heartbeat + /browser/event endpoints. The backend was built
 * for a Chrome extension that never shipped; this module is the native
 * replacement so the admin dashboard's rich per-domain view lights up.
 *
 * Design notes:
 *   • PowerShell + UIAutomationClient.dll is used for URL extraction —
 *     same dependency surface as the existing getForegroundApp() in main.js
 *     (no new native modules to package).
 *   • UIA tree walks are slow (50–500 ms on cold runs), so the URL is
 *     fetched at a low cadence (BROWSER_POLL_INTERVAL) and only when the
 *     foreground process is actually a known browser.
 *   • Tab transitions emit /browser/event with the *previous* tab's
 *     duration + counter deltas; the current tab is reported live via
 *     /browser/heartbeat.
 *   • Privacy: incognito / private-browsing windows are skipped entirely;
 *     a configurable list of sensitive domains is redacted before send.
 */

const { execFile } = require("child_process");
const { promisify } = require("util");
const execFileP = promisify(execFile);
const crypto = require("crypto");

// ─── CONFIG ──────────────────────────────────────────────────────────────────
const BROWSER_POLL_INTERVAL = 20 * 1000; // poll active tab every 20 s (was 10 s) — halves the costly UIA tree-walks / PowerShell spawns
const HEARTBEAT_FLUSH_MS = 30 * 1000;    // /browser/heartbeat at most every 30 s
const UIA_TIMEOUT_MS = 6000;             // PowerShell UIA query timeout
const URL_CACHE_MS = 18000;              // dedupe consecutive identical URL reads (was 8 s; widened to cover one poll cycle)
const MIN_TAB_DURATION_SEC = 2;          // ignore tab "flashes" shorter than this

const BROWSER_PROCESSES = new Set([
  "chrome", "msedge", "edge", "firefox", "brave", "opera", "vivaldi", "arc",
  "thorium", "chromium",
]);

// Window-title patterns that mean "do not capture this tab" — incognito /
// private browsing across all major browsers. Matched case-insensitively.
const INCOGNITO_PATTERNS = [
  /\(incognito\)/i,           // Chrome / Brave / Vivaldi
  /\binprivate\b/i,           // Edge
  /private browsing/i,        // Firefox
  /\bprivate window\b/i,      // Safari-ish (rare on Windows)
];

// Domains that should never be sent in clear: titles + URLs are redacted to
// a deterministic short hash so duration accounting still works without
// leaking what the employee was actually looking at.
const SENSITIVE_DOMAINS = [
  // Personal banking / payments
  /\bbank\b/i, /paypal\.com$/i, /chase\.com$/i, /wellsfargo\.com$/i,
  /hdfcbank\.com$/i, /icicibank\.com$/i, /sbi\.co\.in$/i, /axisbank\.com$/i,
  /citibank\.com$/i, /americanexpress\.com$/i, /capitalone\.com$/i,
  /revolut\.com$/i, /wise\.com$/i,
  // Healthcare / medical
  /myhealth/i, /patient\./i, /mychart/i, /webmd\.com$/i,
  // Personal email (best-effort — work email lives on same domains, so this
  // is intentionally narrow; admins can tighten via the future settings UI)
  /^mail\.proton\.me$/i, /^mail\.tutanota\.com$/i, /^login\.live\.com$/i,
];

// ─── STATE ───────────────────────────────────────────────────────────────────
let pollTimer = null;
let currentTab = null;        // { url, domain, tab_title, favicon_url, browser,
                              //   started_at, last_heartbeat_at,
                              //   key_baseline, click_baseline, idle_seconds }
let _urlCache = null;         // { value, ts }
let _lastFgTitle = null;      // last foreground window title we walked UIA for
let opts = null;              // { apiBase, getAuthToken, getCounters, log }

// ─── UTILITIES ───────────────────────────────────────────────────────────────
function _domainOf(url) {
  if (!url) return "";
  try {
    const u = new URL(url);
    return (u.hostname || "").toLowerCase().replace(/^www\./, "");
  } catch {
    return "";
  }
}

function _isSensitive(domain) {
  if (!domain) return false;
  return SENSITIVE_DOMAINS.some((re) => re.test(domain));
}

function _isIncognito(windowTitle) {
  if (!windowTitle) return false;
  return INCOGNITO_PATTERNS.some((re) => re.test(windowTitle));
}

function _redactUrl(url) {
  const h = crypto.createHash("sha256").update(url || "").digest("hex").slice(0, 12);
  return `https://redacted.sensitive/${h}`;
}

function _faviconFor(domain) {
  if (!domain) return null;
  // Google's S2 favicon service — no per-site network call from the tracker.
  return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=64`;
}

function _normaliseBrowser(procName) {
  const p = (procName || "").toLowerCase();
  if (p.startsWith("msedge") || p === "edge") return "edge";
  if (p.startsWith("chrome") || p === "chromium") return "chrome";
  if (p === "firefox") return "firefox";
  if (p === "brave") return "brave";
  if (p === "opera") return "opera";
  if (p === "vivaldi") return "vivaldi";
  if (p === "arc") return "arc";
  return p || "browser";
}

// ─── UIA URL EXTRACTION (Win32 / PowerShell) ─────────────────────────────────
// The script attaches to the foreground window via UIA, walks for the
// address-bar Edit control, and returns its ValuePattern.Value. Each
// browser exposes the URL bar slightly differently — the script tries a
// few selectors in order of specificity, then falls back to "first Edit
// with a non-empty value that looks URL-ish".
const URL_PS_SCRIPT = `
Add-Type -AssemblyName UIAutomationClient -ErrorAction SilentlyContinue
Add-Type -AssemblyName UIAutomationTypes  -ErrorAction SilentlyContinue
Add-Type -AssemblyName WindowsBase        -ErrorAction SilentlyContinue
$sig = @'
[DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
'@
Add-Type -MemberDefinition $sig -Name Win32Url -Namespace ZtNative -ErrorAction SilentlyContinue | Out-Null
try {
  $hwnd = [ZtNative.Win32Url]::GetForegroundWindow()
  if ($hwnd -eq [IntPtr]::Zero) { return "" }
  $root = [System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
  if (-not $root) { return "" }
  $editType = [System.Windows.Automation.ControlType]::Edit
  $cond = New-Object System.Windows.Automation.PropertyCondition([System.Windows.Automation.AutomationElement]::ControlTypeProperty, $editType)
  $edits = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $cond)
  $best = ""
  foreach ($e in $edits) {
    try {
      $name = $e.Current.Name
      $valPat = $e.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
      if (-not $valPat) { continue }
      $v = $valPat.Current.Value
      if ([string]::IsNullOrWhiteSpace($v)) { continue }
      # Prefer the element whose accessibility name signals "address bar".
      if ($name -match 'address|location|url') { return $v }
      # Keep the first plausible URL-ish value as a fallback.
      if ($best -eq "" -and ($v -match '^[a-zA-Z][a-zA-Z0-9+.-]*://' -or $v -match '^[a-zA-Z0-9.-]+\\.[a-zA-Z]{2,}')) {
        $best = $v
      }
    } catch {}
  }
  return $best
} catch { return "" }
`;

async function _readActiveBrowserUrl() {
  // Reuse a very short cache so back-to-back polls don't both spawn PS.
  if (_urlCache && Date.now() - _urlCache.ts < URL_CACHE_MS) {
    return _urlCache.value;
  }
  try {
    const { stdout } = await execFileP(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-Command", URL_PS_SCRIPT,
      ],
      { encoding: "utf-8", timeout: UIA_TIMEOUT_MS, windowsHide: true },
    );
    const raw = (stdout || "").trim();
    let url = raw;
    // PS may print "https://..." or just "google.com/path" — normalise.
    if (url && !/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(url)) {
      url = "https://" + url;
    }
    // Reject obvious non-URLs (e.g. search box text)
    try { new URL(url); } catch { url = ""; }
    _urlCache = { value: url, ts: Date.now() };
    return url;
  } catch (e) {
    // UIA can throw on locked screens, on first call before assemblies load,
    // or when the browser hasn't finished rendering the URL bar. We treat
    // any failure as "no URL right now" and try again on the next tick.
    return "";
  }
}

// ─── BACKEND CALLS ───────────────────────────────────────────────────────────
async function _post(path, body) {
  if (!opts) return;
  const token = opts.getAuthToken && opts.getAuthToken();
  if (!token) return;
  try {
    const fetchFn = (await import("node-fetch")).default;
    await fetchFn(`${opts.apiBase}${path}`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${token}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      timeout: 8000,
    });
  } catch (e) {
    // Browser activity is best-effort — never log loudly. The dashboard
    // just shows slightly stale data if a heartbeat misses.
    if (opts.log) opts.log(`[browser] ${path} failed: ${e.message}`);
  }
}

function _redactedPayload(tab) {
  // Build a payload that preserves duration + counters but hides what
  // domain / URL the employee was on. The dashboard will render this as
  // a generic "redacted.sensitive" row, which makes the privacy posture
  // visible to admins (rather than silently dropping the time).
  return {
    url: _redactUrl(tab.url || ""),
    domain: "redacted.sensitive",
    tab_title: "Sensitive site (redacted)",
    favicon_url: null,
    browser: tab.browser,
  };
}

function _publicPayload(tab) {
  // track_urls off → keep domain-level visibility only (which browser, which
  // site, for how long) but never send the full URL/path or page title.
  const captureUrls = !opts || !opts.getCaptureUrls || opts.getCaptureUrls();
  if (!captureUrls) {
    const domain = _domainOf(tab.url || "");
    return {
      url: "",
      domain,
      tab_title: domain,
      favicon_url: null,
      browser: tab.browser,
    };
  }
  return {
    url: tab.url,
    tab_title: tab.tab_title,
    favicon_url: tab.favicon_url,
    browser: tab.browser,
  };
}

async function _sendHeartbeat(tab, isIdle) {
  const payload = tab._sensitive ? _redactedPayload(tab) : _publicPayload(tab);
  const counters = opts.getCounters ? opts.getCounters() : { keys: 0, clicks: 0 };
  const keys = Math.max(0, counters.keys - (tab.key_baseline || 0));
  const clicks = Math.max(0, counters.clicks - (tab.click_baseline || 0));
  await _post("/browser/heartbeat", {
    ...payload,
    is_idle: !!isIdle,
    tab_since: new Date(tab.started_at).toISOString(),
    mouse_clicks: clicks,
    key_presses: keys,
    scrolls: 0, // not tracked natively yet — extension-only field
  });
  tab.last_heartbeat_at = Date.now();
}

async function _sendEvent(tab) {
  const ended = Date.now();
  const durationSec = Math.floor((ended - tab.started_at) / 1000);
  if (durationSec < MIN_TAB_DURATION_SEC) return; // ignore flash transitions

  const payload = tab._sensitive ? _redactedPayload(tab) : _publicPayload(tab);
  const counters = opts.getCounters ? opts.getCounters() : { keys: 0, clicks: 0 };
  const keys = Math.max(0, counters.keys - (tab.key_baseline || 0));
  const clicks = Math.max(0, counters.clicks - (tab.click_baseline || 0));
  const idleSec = Math.min(durationSec, tab.idle_seconds || 0);
  const activeSec = Math.max(0, durationSec - idleSec);

  await _post("/browser/event", {
    ...payload,
    started_at: new Date(tab.started_at).toISOString(),
    ended_at: new Date(ended).toISOString(),
    active_sec: activeSec,
    idle_sec: idleSec,
    mouse_clicks: clicks,
    key_presses: keys,
    scrolls: 0,
  });
}

// ─── POLL LOOP ───────────────────────────────────────────────────────────────
async function _pollOnce() {
  if (!opts || !opts.isTracking || !opts.isTracking()) return;

  // Step 1: figure out what the foreground app is. main.js already has a
  // cached helper — we receive it via opts so we don't duplicate PowerShell.
  let fg = null;
  try {
    fg = await opts.getForeground();
  } catch {
    fg = null;
  }
  if (!fg) return;

  const procName = (fg.appName || "").toLowerCase();
  const isBrowser = BROWSER_PROCESSES.has(procName);

  // Foreground isn't a browser → close out any open tab session and bail.
  if (!isBrowser) {
    if (currentTab) {
      await _sendEvent(currentTab);
      currentTab = null;
    }
    return;
  }

  // Foreground IS a browser, but it's incognito → close any open public tab
  // session and skip capture entirely (privacy guardrail).
  if (_isIncognito(fg.windowTitle)) {
    if (currentTab) {
      await _sendEvent(currentTab);
      currentTab = null;
    }
    return;
  }

  // Cheap gate BEFORE the expensive UIA walk: if the foreground window title is
  // unchanged since the last tab we captured and we already have an open tab
  // session, the user is still on the same tab. Skip the 50–500 ms UIA tree-walk
  // entirely and just keep the existing session warm via the heartbeat
  // rate-limiter. (A tab/page switch almost always changes the window title, so
  // this is a safe heuristic; URL-only changes within the same title are caught
  // on the next title change.)
  const fgTitle = fg.windowTitle || "";
  if (currentTab && fgTitle && fgTitle === _lastFgTitle) {
    if (Date.now() - currentTab.last_heartbeat_at >= HEARTBEAT_FLUSH_MS) {
      const stillIdle = opts.isIdle ? opts.isIdle() : false;
      if (stillIdle) currentTab.idle_seconds += BROWSER_POLL_INTERVAL / 1000;
      await _sendHeartbeat(currentTab, stillIdle);
    }
    return;
  }

  // Step 2: extract URL via UIA. If we can't, fall back to the window title
  // (still useful — backend categorises by domain, and many tab titles
  // include "Google Chrome — github.com" or similar — but only if we can
  // parse a domain out of it).
  const url = await _readActiveBrowserUrl();
  const domain = _domainOf(url);
  if (!url || !domain) {
    // No URL → can't categorise; leave the previous tab session running so
    // we don't double-count "Chrome → Chrome" as a tab switch when UIA
    // momentarily fails.
    return;
  }

  // We successfully walked for this title — remember it so the next poll can
  // skip the UIA walk if the user is still on the same tab (see gate above).
  _lastFgTitle = fgTitle;

  // Strip the trailing " — Google Chrome" / " — Mozilla Firefox" / etc. so
  // the dashboard shows the actual page title, not the browser name.
  let tabTitle = (fg.windowTitle || "")
    .replace(/\s+[-—–|]\s+(Google Chrome|Mozilla Firefox|Microsoft.\s*Edge|Brave|Opera|Vivaldi)\s*$/i, "")
    .trim();
  if (!tabTitle) tabTitle = domain;

  // Step 3: tab-change detection. The cheapest reliable identity for a
  // "tab session" is (origin + path) — querystring changes inside the same
  // SPA shouldn't count as a tab switch (otherwise React Router navigation
  // would inflate transition counts wildly).
  const sensitive = _isSensitive(domain);
  const sameTab =
    currentTab &&
    _domainOf(currentTab.url) === domain &&
    (() => {
      try {
        return new URL(currentTab.url).pathname === new URL(url).pathname;
      } catch {
        return currentTab.url === url;
      }
    })();

  if (!sameTab) {
    // Close the previous tab session, if any.
    if (currentTab) {
      await _sendEvent(currentTab);
    }
    // Start a fresh tab session.
    const counters = opts.getCounters ? opts.getCounters() : { keys: 0, clicks: 0 };
    currentTab = {
      url,
      domain,
      tab_title: tabTitle,
      favicon_url: _faviconFor(domain),
      browser: _normaliseBrowser(procName),
      started_at: Date.now(),
      last_heartbeat_at: 0,
      key_baseline: counters.keys || 0,
      click_baseline: counters.clicks || 0,
      idle_seconds: 0,
      _sensitive: sensitive,
    };
    // Fire an immediate heartbeat so the dashboard sees the new tab within
    // ~1 poll interval instead of waiting up to HEARTBEAT_FLUSH_MS.
    await _sendHeartbeat(currentTab, false);
    return;
  }

  // Same tab still active → rate-limit heartbeats to keep DB writes cheap.
  if (Date.now() - currentTab.last_heartbeat_at >= HEARTBEAT_FLUSH_MS) {
    const isIdle = opts.isIdle ? opts.isIdle() : false;
    if (isIdle) currentTab.idle_seconds += BROWSER_POLL_INTERVAL / 1000;
    await _sendHeartbeat(currentTab, isIdle);
  }
}

// ─── PUBLIC API ──────────────────────────────────────────────────────────────
function start(options) {
  // Required options from main.js:
  //   apiBase        — e.g. "https://app.temveris.example"
  //   getAuthToken   — () => current JWT or null
  //   getForeground  — async () => { appName, windowTitle }  (reuses main.js cache)
  //   getCounters    — () => { keys, clicks }                (global input hook totals)
  //   isTracking     — () => bool                            (gate everything on clock-in)
  //   isIdle         — () => bool                            (optional; defaults to false)
  //   log            — (msg) => void                         (optional)
  opts = options;
  if (pollTimer) clearInterval(pollTimer);
  // First tick after a short delay so the foreground/auth state is settled.
  setTimeout(() => { _pollOnce().catch(() => {}); }, 4 * 1000);
  pollTimer = setInterval(() => { _pollOnce().catch(() => {}); }, BROWSER_POLL_INTERVAL);
  if (opts.log) opts.log("[browser] tracking started");
}

async function stop() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  // Flush the in-flight tab session so its duration isn't lost.
  if (currentTab) {
    try { await _sendEvent(currentTab); } catch {}
    currentTab = null;
  }
  _urlCache = null;
  _lastFgTitle = null;
  if (opts && opts.log) opts.log("[browser] tracking stopped");
}

module.exports = { start, stop };
