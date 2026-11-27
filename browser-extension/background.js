/**
 * Temveris Tracker — background service worker (MV3)
 * ====================================================
 * Responsibilities:
 *   • Track which tab is active in which window, and in which browser.
 *   • Maintain an "open session" for the active tab (url, title, favicon,
 *     started_at, interaction counters) and close it when the focus moves.
 *   • Send heartbeats every HEARTBEAT_SEC so the dashboard's live state
 *     never goes stale.
 *   • Respect system idle: when the OS reports idle/locked for IDLE_THRESHOLD
 *     seconds, split the current session (idle_sec grows, active_sec frozen).
 *
 * All POSTs go to {apiBase}/browser/{heartbeat,event} with
 * Authorization: Bearer {token} — both configured from the popup.
 */

const HEARTBEAT_SEC = 15;
const IDLE_THRESHOLD = 60; // seconds before the OS reports "idle"
const STORAGE_KEYS = { apiBase: "apiBase", token: "token" };

// ── Active session state (kept in memory; service worker may be torn down,
// so we also persist a light snapshot to chrome.storage for recovery.) ──
let current = null;
// Shape of `current`:
// {
//   tabId, windowId, url, domain, tabTitle, favIconUrl, browser,
//   startedAt: ISO, lastHeartbeatAt: ISO,
//   activeSec, idleSec, mouseClicks, keyPresses, scrolls,
//   isIdle: bool
// }

const browserName = (() => {
  // navigator isn't available in MV3 service workers — infer from UA-like info.
  // The extension doesn't need a perfect browser name, just a stable label.
  try {
    if (typeof navigator !== "undefined" && navigator.userAgent) {
      const ua = navigator.userAgent.toLowerCase();
      if (ua.includes("edg/")) return "edge";
      if (ua.includes("firefox")) return "firefox";
      if (ua.includes("chrome")) return "chrome";
    }
  } catch (_) {}
  return "chromium";
})();

function domainOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, "").toLowerCase(); }
  catch { return "unknown"; }
}

function isTrackable(url) {
  if (!url) return false;
  // Skip internal pages — they add noise and we can't content-script them.
  return /^https?:\/\//i.test(url);
}

async function getConfig() {
  const { apiBase, token } = await chrome.storage.local.get([
    STORAGE_KEYS.apiBase,
    STORAGE_KEYS.token,
  ]);
  return { apiBase: (apiBase || "").replace(/\/+$/, ""), token };
}

async function post(path, body) {
  const { apiBase, token } = await getConfig();
  if (!apiBase || !token) return; // silently skip until configured
  try {
    const res = await fetch(`${apiBase}${path}`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      console.warn("[Temveris] POST", path, "→", res.status);
    }
  } catch (e) {
    console.warn("[Temveris] POST failed", path, e);
  }
}

async function openSession(tab) {
  if (!isTrackable(tab.url)) { current = null; return; }
  const now = new Date().toISOString();
  current = {
    tabId:       tab.id,
    windowId:    tab.windowId,
    url:         tab.url,
    domain:      domainOf(tab.url),
    tabTitle:    tab.title || "",
    favIconUrl:  tab.favIconUrl || null,
    browser:     browserName,
    startedAt:   now,
    lastHeartbeatAt: now,
    activeSec:   0,
    idleSec:     0,
    mouseClicks: 0,
    keyPresses:  0,
    scrolls:     0,
    isIdle:      false,
  };
  await chrome.storage.session?.set?.({ current }).catch(() => {});
  await sendHeartbeat(true);
}

async function closeSession(reason = "switch") {
  if (!current) return;
  const endedAt = new Date().toISOString();
  const body = {
    url:          current.url,
    tab_title:    current.tabTitle,
    favicon_url:  current.favIconUrl,
    browser:      current.browser,
    started_at:   current.startedAt,
    ended_at:     endedAt,
    active_sec:   current.activeSec,
    idle_sec:     current.idleSec,
    mouse_clicks: current.mouseClicks,
    key_presses:  current.keyPresses,
    scrolls:      current.scrolls,
  };
  current = null;
  await chrome.storage.session?.remove?.("current").catch(() => {});
  await post("/browser/event", body);
}

async function sendHeartbeat(initial = false) {
  if (!current) return;
  const now = Date.now();
  const lastHb = new Date(current.lastHeartbeatAt).getTime();
  const elapsed = Math.max(0, Math.round((now - lastHb) / 1000));
  if (!initial) {
    if (current.isIdle) current.idleSec += elapsed;
    else                current.activeSec += elapsed;
  }
  current.lastHeartbeatAt = new Date(now).toISOString();

  await post("/browser/heartbeat", {
    url:          current.url,
    tab_title:    current.tabTitle,
    favicon_url:  current.favIconUrl,
    browser:      current.browser,
    is_idle:      current.isIdle,
    tab_since:    current.startedAt,
    mouse_clicks: current.mouseClicks,
    key_presses:  current.keyPresses,
    scrolls:      current.scrolls,
  });

  // Reset per-heartbeat counters so the backend sees interactions per window.
  current.mouseClicks = 0;
  current.keyPresses  = 0;
  current.scrolls     = 0;
  await chrome.storage.session?.set?.({ current }).catch(() => {});
}

// ──────────────────────────────────────────────
// Tab / window focus events
// ──────────────────────────────────────────────
async function handleActiveTabChange() {
  // Resolve which tab is actually focused across all normal windows.
  const win = await chrome.windows.getLastFocused({ populate: true }).catch(() => null);
  if (!win || win.type !== "normal") {
    // Browser lost focus (user alt-tabbed out) — close current session.
    await closeSession("blur");
    return;
  }
  const active = (win.tabs || []).find(t => t.active);
  if (!active) { await closeSession("no-tab"); return; }
  if (current && current.tabId === active.id && current.url === active.url) return;
  await closeSession("switch");
  await openSession(active);
}

chrome.tabs.onActivated.addListener(() => { handleActiveTabChange(); });
chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (!tab.active) return;
  // URL/title/favicon changed on the active tab → treat as a new session when
  // the URL changed; otherwise just update our cached metadata.
  if (changeInfo.url) { handleActiveTabChange(); return; }
  if (current && current.tabId === tabId) {
    if (changeInfo.title)      current.tabTitle    = changeInfo.title;
    if (changeInfo.favIconUrl) current.favIconUrl = changeInfo.favIconUrl;
  }
});
chrome.windows.onFocusChanged.addListener(() => { handleActiveTabChange(); });
chrome.tabs.onRemoved.addListener((tabId) => {
  if (current && current.tabId === tabId) closeSession("closed");
});

// ──────────────────────────────────────────────
// Idle detection (OS-level)
// ──────────────────────────────────────────────
chrome.idle.setDetectionInterval(IDLE_THRESHOLD);
chrome.idle.onStateChanged.addListener((state) => {
  if (!current) return;
  current.isIdle = (state !== "active");
});

// ──────────────────────────────────────────────
// Heartbeat alarm
// ──────────────────────────────────────────────
chrome.alarms.create("zt-heartbeat", { periodInMinutes: HEARTBEAT_SEC / 60 });
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === "zt-heartbeat") sendHeartbeat(false);
});

// ──────────────────────────────────────────────
// Interaction counters from content scripts
// ──────────────────────────────────────────────
chrome.runtime.onMessage.addListener((msg, sender) => {
  if (!current || !sender.tab || sender.tab.id !== current.tabId) return;
  if (msg?.type !== "zt-interaction") return;
  current.mouseClicks += msg.clicks || 0;
  current.keyPresses  += msg.keys   || 0;
  current.scrolls     += msg.scrolls || 0;
  // A user interaction also counts as "not idle" in the extension's view,
  // even if the OS idle state hasn't caught up yet.
  current.isIdle = false;
});

// ──────────────────────────────────────────────
// Service-worker restart: try to recover in-flight session so we don't
// lose the current tab interval between SW wake-ups.
// ──────────────────────────────────────────────
(async () => {
  const { current: saved } = await chrome.storage.session?.get?.("current") || {};
  if (saved) current = saved;
  handleActiveTabChange();
})();

// Pause/close session if the whole browser is shutting down.
chrome.runtime.onSuspend?.addListener(() => { closeSession("suspend"); });
