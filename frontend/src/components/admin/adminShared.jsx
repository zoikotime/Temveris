// Shared C-free admin presentational/utility layer — extracted from
// AdminDashboard.jsx (Phase 4a). Contains NO dependency on the module-level
// mutable theme `C`; the C-coupled components (Icon, EmpCard, TimelineView)
// intentionally remain in AdminDashboard until the Context phase (4b).
import React from "react";
import { fmtTime as _fmtTime, dateKey as _dateKey, getOrgTimeZone } from "../../lib/orgTime";

// Time formatting is delegated to lib/orgTime, which renders on the
// ORGANIZATION's configured clock rather than a hardcoded Asia/Kolkata. The
// `IST` names are kept so the ~30 call sites across the dashboard do not all
// have to change in the same commit; they are aliases, not behaviour.
export { parseUTC } from "../../lib/orgTime";
export const fmtTimeIST = _fmtTime;
export const istDateKey = (d) => _dateKey(d);

export const effectiveStatus = (emp) => {
  if (!emp) return "";
  const raw = (emp.status || "").toLowerCase();
  const terminal = ["stopped", "stopped work", "stop", "offline"];
  if (!terminal.includes(raw)) return emp.status;

  // "Absent" is a POLICY verdict, not a date comparison. The server resolves
  // it per employee against their working days, absence cutoff and leave
  // (schedule_policy/annotate.py) and ships it as `arrival_status`. This used
  // only "their last session was not today", so a Mon-Fri employee read as
  // Absent on a Saturday, and anyone on approved leave read as Absent too.
  if (emp.arrival_status === "absent") return "absent";
  if (emp.is_working_day === false) return emp.status;   // not their day at all
  if (emp.arrival_status) return emp.status;             // server spoke; trust it

  // No server verdict (a payload predating the resolver, or a resolve
  // failure): fall back to the old heuristic rather than showing nothing.
  const today = istDateKey(new Date());
  const ref = emp.clock_out_time || emp.clock_in_time || emp.last_seen;
  if (ref && istDateKey(ref) !== today) return "absent";
  return emp.status;
};

// SHIFT_NOON_HOUR and _istNow were removed in P9. Both existed only to support
// the client-side late/absent derivation: the noon cutoff is the server's
// `schedule.absent_after_hour`, and attendance arithmetic belongs to the
// backend Schedule Resolver. What remains is DISPLAY formatting, which now
// follows the organization's configured timezone.
export const _IST_TZ = getOrgTimeZone;


export const _formatLateLabel = (mins) => {
  if (!Number.isFinite(mins) || mins <= 0) return null;
  const m = Math.floor(mins);
  if (m < 60) return `Late by ${m} min`;
  const h  = Math.floor(m / 60);
  const mr = m % 60;
  if (mr === 0) return `Late by ${h} hr`;
  return `Late by ${h} hr ${mr} min`;
};

// Maps the server's `arrival_status` onto the phase vocabulary this dashboard
// already renders. The backend Schedule Resolver is the ONLY source of truth for
// late / very-late / expected-hours — see app/schedule_policy/. This function
// deliberately performs NO date, grace or threshold arithmetic; it is a lookup.
//
// Before P9 it re-derived everything client-side from `default_clock_in_time`
// with a hardcoded Sat/Sun weekend and a `?? 20` grace default, which silently
// disagreed with the server the moment an org configured anything.
const ARRIVAL_TO_PHASE = {
  not_applicable: "no_shift",
  awaiting:       "before_shift",
  within_grace:   "within_grace",
  present:        "within_grace",
  late:           "late_present",
  very_late:      "very_late",
  late_pending:   "late_pending",
  absent:         "absent_after_noon",
};

export const computeShiftInfo = (emp) => {
  const empty = { hasShift: false, shiftStart: null, graceEnd: null, lateMinutes: null, lateLabel: null, phase: "no_shift" };
  if (!emp) return empty;

  // Server-resolved. `arrival_status` is absent only for a payload that predates
  // P9 (or a resolver failure), in which case we show no shift rather than
  // falling back to a second implementation.
  const status = emp.arrival_status;
  if (!status || status === "not_applicable") return empty;

  const lateMinutes = Number.isFinite(Number(emp.late_minutes))
    ? Number(emp.late_minutes) : null;

  return {
    hasShift: true,
    shiftStart: emp.shift_start_time || null,
    graceEnd: null,                       // no longer computed client-side
    lateMinutes,
    lateLabel: _formatLateLabel(lateMinutes),
    phase: ARRIVAL_TO_PHASE[status] || "no_shift",
    arrivalStatus: status,
  };
};


export const initials = (name) =>
  (name || "").split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2) || "U";

export const spawnRipple = (e) => {
  try {
    const btn = e.currentTarget;
    if (!btn) return;
    const rect = btn.getBoundingClientRect();
    const span = document.createElement("span");
    span.className = "zt-ripple";
    span.style.left = (e.clientX - rect.left - 7) + "px";
    span.style.top  = (e.clientY - rect.top  - 7) + "px";
    btn.appendChild(span);
    setTimeout(() => span.remove(), 600);
  } catch { /* no-op */ }
};

export const APP_META = [
  // [pattern, label, simple-icons slug, brand color]
  // Microsoft revoked the CC0 license for their brand icons on Simple Icons
  // (see simple-icons/simple-icons#11271), so every `microsoft*` slug — plus
  // `visualstudiocode` — now 404s on the CDN. Keep these as `null` so the
  // colored letter-tile fallback renders instead of spamming the console.
  [/\b(code|vscode|visual ?studio ?code)\b/i, "VS Code",        null,                   "#007ACC"],
  [/\bchrome\b/i,                              "Chrome",         "googlechrome",         "#4285F4"],
  [/\bfirefox\b/i,                             "Firefox",        "firefoxbrowser",       "#FF7139"],
  [/\b(msedge|edge)\b/i,                       "Edge",           null,                   "#0078D7"],
  [/\bsafari\b/i,                              "Safari",         "safari",               "#006CFF"],
  [/\bopera\b/i,                               "Opera",          "opera",                "#FF1B2D"],
  [/\bbrave\b/i,                               "Brave",          "brave",                "#FB542B"],
  [/\bfigma\b/i,                               "Figma",          "figma",                "#F24E1E"],
  [/\bslack\b/i,                               "Slack",          "slack",                "#4A154B"],
  [/\bdiscord\b/i,                             "Discord",        "discord",              "#5865F2"],
  [/\bteams\b/i,                               "MS Teams",       null,                   "#6264A7"],
  [/\bzoom\b/i,                                "Zoom",           "zoom",                 "#2D8CFF"],
  [/\bnotion\b/i,                              "Notion",         "notion",               "#000000"],
  [/\bobsidian\b/i,                            "Obsidian",       "obsidian",             "#7C3AED"],
  [/\bpostman\b/i,                             "Postman",        "postman",              "#FF6C37"],
  [/\bspotify\b/i,                             "Spotify",        "spotify",              "#1DB954"],
  [/\bwhatsapp\b/i,                            "WhatsApp",       "whatsapp",             "#25D366"],
  [/\btelegram\b/i,                            "Telegram",       "telegram",             "#26A5E4"],
  [/\boutlook\b/i,                             "Outlook",        null,                   "#0078D4"],
  [/\b(winword|msword|word)\b/i,               "Word",           null,                   "#2B579A"],
  [/\bexcel\b/i,                               "Excel",          null,                   "#217346"],
  [/\b(powerpnt|powerpoint)\b/i,               "PowerPoint",     null,                   "#B7472A"],
  [/\bonenote\b/i,                             "OneNote",        null,                   "#7719AA"],
  [/\bdocker\b/i,                              "Docker",         "docker",               "#2496ED"],
  [/\bgithub\b/i,                              "GitHub",         "github",               "#181717"],
  [/\bgitlab\b/i,                              "GitLab",         "gitlab",               "#FC6D26"],
  [/\bandroid ?studio\b/i,                     "Android Studio", "androidstudio",        "#3DDC84"],
  [/\bintellij\b/i,                            "IntelliJ",       "intellijidea",         "#000000"],
  [/\bpycharm\b/i,                             "PyCharm",        "pycharm",              "#21D789"],
  [/\bsublime\b/i,                             "Sublime",        "sublimetext",          "#FF9800"],
  [/\b(atom)\b/i,                              "Atom",           "atom",                 "#66595C"],
  [/\b(vim|neovim|nvim)\b/i,                   "Vim",            "vim",                  "#019733"],
  [/\bwebstorm\b/i,                            "WebStorm",       "webstorm",             "#000000"],
  // Known-but-no-slug: render a colored letter tile with the friendly label
  [/\bworkfolio\b/i,                           "Workfolio",      null,                   "#0EA5E9"],
  [/\b(explorer|file ?explorer)\b/i,           "File Explorer",  null,                   "#F6A623"],
  [/\b(cmd|powershell|pwsh|bash|terminal|windowsterminal|wt)\b/i, "Terminal", null,      "#111827"],
  [/\b(slackcall|meet|google ?meet)\b/i,       "Google Meet",    "googlemeet",           "#00897B"],
];

// Desktop process names that mean "the foreground app is a web browser", so a
// captured tab favicon is safe to render in place of the browser's own logo.
// Mirrors _BROWSER_RE in backend/app/employees/router.py. The guard matters
// because /tracking/heartbeat overwrites LiveStatus.app_name WITHOUT clearing
// url/domain/tab_title/favicon_url — so after a worker alt-tabs from Chrome to
// VS Code, the row still carries the last tab's favicon. Without this check a
// stale github.com icon would end up glued to a "VS Code" chip.
export const BROWSER_APP_RE = /\b(chrome|edge|msedge|firefox|safari|brave|opera|vivaldi|arc)\b/i;

// The site favicon to show for an app chip, or null to fall back to the app
// icon. `tab` is the `current_tab` object from /employees/team
// ({ domain, tab_title, favicon_url, ... }), which is null when the browser
// extension has never reported for this user.
export function getTabFavicon(rawAppName, tab) {
  const url = tab && tab.favicon_url;
  if (!url || !rawAppName) return null;
  if (!BROWSER_APP_RE.test(String(rawAppName))) return null;
  // Extension favicons are http(s) or data: URIs. Anything else (a
  // chrome-extension:// internal page, say) can never load in the admin's
  // browser, so skip it rather than render a broken image for a frame.
  if (!/^(https?:|data:image\/)/i.test(url)) return null;
  return url;
}

export function getAppMeta(rawName) {
  if (!rawName) return null;
  const cleaned = String(rawName).trim().replace(/\.exe$/i, "");
  for (const [re, label, slug, color] of APP_META) {
    if (re.test(cleaned)) return { label, slug, color };
  }
  const first = (cleaned[0] || "?").toUpperCase();
  const hue = ((first.charCodeAt(0) || 0) * 37) % 360;
  return { label: cleaned, slug: null, color: `hsl(${hue},65%,50%)` };
}

export const AppIcon = ({ app, size = 16, favicon = null }) => {
  const [errored, setErrored] = React.useState(false);
  // Remember WHICH favicon url failed rather than a bare boolean, so switching
  // to a new tab re-tries instead of inheriting the previous site's failure.
  const [favFailed, setFavFailed] = React.useState(null);
  if (!app) return null;
  // Prefer the real site favicon when the caller supplied one (browser chips),
  // falling back to the app icon below if the image 404s or is blocked.
  if (favicon && favFailed !== favicon) {
    return (
      <img
        src={favicon}
        alt={app.label}
        width={size}
        height={size}
        loading="lazy"
        decoding="async"
        referrerPolicy="no-referrer"
        onError={() => setFavFailed(favicon)}
        style={{ display: "block", flexShrink: 0, objectFit: "contain" }}
      />
    );
  }
  if (app.slug && !errored) {
    const hex = (app.color || "#000000").replace("#", "");
    return (
      <img
        src={`https://cdn.simpleicons.org/${app.slug}/${hex}`}
        alt={app.label}
        width={size}
        height={size}
        onError={() => setErrored(true)}
        style={{ display: "block", flexShrink: 0, objectFit: "contain" }}
      />
    );
  }
  return (
    <span style={{
      width: size, height: size, borderRadius: Math.max(3, Math.round(size / 4)),
      background: app.color,
      display: "inline-flex", alignItems: "center", justifyContent: "center",
      fontSize: Math.max(8, size - 7), color: "#fff", fontWeight: 800,
      flexShrink: 0, textTransform: "uppercase", lineHeight: 1,
    }}>
      {(app.label[0] || "?").toUpperCase()}
    </span>
  );
};

export const STATUS_CONFIG = {
  // Working — Green 600
  working:        { key: "working",     label: "Working",      color: "#166534", bg: "#DCFCE7", border: "#86EFAC", dot: "#16A34A", cardBg: "#F0FDF4", cardBorder: "#86EFAC" },
  // On Break — Amber 500
  on_break:       { key: "on_break",    label: "On Break",     color: "#92400E", bg: "#FEF3C7", border: "#FCD34D", dot: "#F59E0B", cardBg: "#FFFBEB", cardBorder: "#FCD34D" },
  "on break":     { key: "on_break",    label: "On Break",     color: "#92400E", bg: "#FEF3C7", border: "#FCD34D", dot: "#F59E0B", cardBg: "#FFFBEB", cardBorder: "#FCD34D" },
  break:          { key: "on_break",    label: "On Break",     color: "#92400E", bg: "#FEF3C7", border: "#FCD34D", dot: "#F59E0B", cardBg: "#FFFBEB", cardBorder: "#FCD34D" },
  // Idle — Gray 400 (replaces legacy "Stopped/Offline" labels)
  idle:           { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  stopped:        { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  "stopped work": { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  stop:           { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  offline:        { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  inactive:       { key: "idle",        label: "Stopped Working", color: "#374151", bg: "#F3F4F6", border: "#D1D5DB", dot: "#9CA3AF", cardBg: "#F9FAFB", cardBorder: "#D1D5DB" },
  // In Meeting — Teal 500
  in_meeting:     { key: "in_meeting",  label: "In Meeting",   color: "#0F766E", bg: "#CCFBF1", border: "#5EEAD4", dot: "#14B8A6", cardBg: "#F0FDFA", cardBorder: "#5EEAD4" },
  "in meeting":   { key: "in_meeting",  label: "In Meeting",   color: "#0F766E", bg: "#CCFBF1", border: "#5EEAD4", dot: "#14B8A6", cardBg: "#F0FDFA", cardBorder: "#5EEAD4" },
  meeting:        { key: "in_meeting",  label: "In Meeting",   color: "#0F766E", bg: "#CCFBF1", border: "#5EEAD4", dot: "#14B8A6", cardBg: "#F0FDFA", cardBorder: "#5EEAD4" },
  // Not Started — Blue 500 (replaces legacy "Yet to Start")
  not_started:    { key: "not_started", label: "Awaiting Clock-In",  color: "#1D4ED8", bg: "#DBEAFE", border: "#93C5FD", dot: "#3B82F6", cardBg: "#EFF6FF", cardBorder: "#93C5FD" },
  "not started":  { key: "not_started", label: "Awaiting Clock-In",  color: "#1D4ED8", bg: "#DBEAFE", border: "#93C5FD", dot: "#3B82F6", cardBg: "#EFF6FF", cardBorder: "#93C5FD" },
  "yet to start": { key: "not_started", label: "Awaiting Clock-In",  color: "#1D4ED8", bg: "#DBEAFE", border: "#93C5FD", dot: "#3B82F6", cardBg: "#EFF6FF", cardBorder: "#93C5FD" },
  yet_to_start:   { key: "not_started", label: "Awaiting Clock-In",  color: "#1D4ED8", bg: "#DBEAFE", border: "#93C5FD", dot: "#3B82F6", cardBg: "#EFF6FF", cardBorder: "#93C5FD" },
  // Late — Amber 600. Arrived after the grace window (server `arrival_status`).
  late:           { key: "late",        label: "Late",         color: "#B45309", bg: "#FEF3C7", border: "#FCD34D", dot: "#F59E0B", cardBg: "#FFFBEB", cardBorder: "#FCD34D" },
  // Very Late — Orange 700. Past the org's very-late threshold but still present.
  // A distinct step between Late and Absent, not a redesign: same pill shape and
  // token structure as every other entry here.
  very_late:      { key: "very_late",   label: "Very Late",    color: "#C2410C", bg: "#FFEDD5", border: "#FDBA74", dot: "#EA580C", cardBg: "#FFF7ED", cardBorder: "#FDBA74" },
  "very late":    { key: "very_late",   label: "Very Late",    color: "#C2410C", bg: "#FFEDD5", border: "#FDBA74", dot: "#EA580C", cardBg: "#FFF7ED", cardBorder: "#FDBA74" },
  // Shift Ended — Slate 400
  shift_ended:    { key: "shift_ended", label: "Shift Ended",  color: "#334155", bg: "#F1F5F9", border: "#CBD5E1", dot: "#94A3B8", cardBg: "#F8FAFC", cardBorder: "#CBD5E1" },
  "shift ended":  { key: "shift_ended", label: "Shift Ended",  color: "#334155", bg: "#F1F5F9", border: "#CBD5E1", dot: "#94A3B8", cardBg: "#F8FAFC", cardBorder: "#CBD5E1" },
  shift_end:      { key: "shift_ended", label: "Shift Ended",  color: "#334155", bg: "#F1F5F9", border: "#CBD5E1", dot: "#94A3B8", cardBg: "#F8FAFC", cardBorder: "#CBD5E1" },
  clocked_out:    { key: "shift_ended", label: "Shift Ended",  color: "#334155", bg: "#F1F5F9", border: "#CBD5E1", dot: "#94A3B8", cardBg: "#F8FAFC", cardBorder: "#CBD5E1" },
  // On Leave — Purple 500
  on_leave:       { key: "on_leave",    label: "On Leave",     color: "#6B21A8", bg: "#F3E8FF", border: "#D8B4FE", dot: "#A855F7", cardBg: "#FAF5FF", cardBorder: "#D8B4FE" },
  "on leave":     { key: "on_leave",    label: "On Leave",     color: "#6B21A8", bg: "#F3E8FF", border: "#D8B4FE", dot: "#A855F7", cardBg: "#FAF5FF", cardBorder: "#D8B4FE" },
  leave:          { key: "on_leave",    label: "On Leave",     color: "#6B21A8", bg: "#F3E8FF", border: "#D8B4FE", dot: "#A855F7", cardBg: "#FAF5FF", cardBorder: "#D8B4FE" },
  // Absent — Red 600 (the only red — reserved per spec)
  absent:         { key: "absent",      label: "Absent",       color: "#991B1B", bg: "#FEE2E2", border: "#FCA5A5", dot: "#DC2626", cardBg: "#FEF2F2", cardBorder: "#FCA5A5" },
  // Scheduled (system-only)
  scheduled:      { key: "scheduled",   label: "Scheduled",    color: "#475569", bg: "#F1F5F9", border: "#CBD5E1", dot: "#64748B", cardBg: "#F8FAFC", cardBorder: "#CBD5E1" },
};

export const getStatusCfg = (s) =>
  STATUS_CONFIG[(s || "").toLowerCase()] || {
    label: s || "Unknown", color: "#667085", bg: "#F9FAFB", border: "#EAECF0",
    dot: "#98A2B3", cardBg: "#FAFAFA", cardBorder: "#E4E7EC",
  };

export const normEmpType = (v) => {
  const s = String(v ?? "").trim().toLowerCase().replace(/[\s-]+/g, "_");
  return s || "full_time";
};

export const KpiSparkline = ({ values = [], color = "#11496A", height = 28 }) => {
  const data = Array.isArray(values) && values.length ? values : [0, 0];
  const max = Math.max(...data, 0.01);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const w = 100;
  const h = height;
  const step = data.length > 1 ? w / (data.length - 1) : 0;
  const points = data.map((v, i) => {
    const x = i * step;
    const y = h - ((v - min) / range) * h;
    return [x, y];
  });
  const linePath = points.map((p, i) => (i === 0 ? `M${p[0]},${p[1]}` : `L${p[0]},${p[1]}`)).join(" ");
  const areaPath = `${linePath} L${w},${h} L0,${h} Z`;
  const lastPt = points[points.length - 1] || [0, h];
  return (
    <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" style={{ display: "block", overflow: "visible" }} aria-hidden>
      <path d={areaPath} fill={color} opacity={0.14} />
      <path d={linePath} fill="none" stroke={color} strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
      <circle cx={lastPt[0]} cy={lastPt[1]} r="2.2" fill={color} />
    </svg>
  );
};

export const Badge = ({ status }) => {
  const cfg = getStatusCfg(status);
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      padding: "2px 8px", borderRadius: 99, fontSize: 11, fontWeight: 700,
      color: cfg.color, background: cfg.bg, border: `1px solid ${cfg.border}`,
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: cfg.dot, display: "inline-block", flexShrink: 0 }} />
      {cfg.label}
    </span>
  );
};

export const ZT_ACCENT = {
  working:   { color: "#00E5A8", textLight: "#047857", glow: "0, 229, 168",   label: "Working"  },
  on_break:  { color: "#FFB547", textLight: "#B45309", glow: "255, 181, 71",  label: "On Break" },
  stopped:   { color: "#7E8AA3", textLight: "#475569", glow: "126, 138, 163", label: "Stopped"  },
  on_leave:  { color: "#7B61FF", textLight: "#6D28D9", glow: "123, 97, 255",  label: "On Leave" },
  absent:    { color: "#FF5470", textLight: "#BE123C", glow: "255, 84, 112",  label: "Absent"   },
  yet:       { color: "#4DA3FF", textLight: "#1D4ED8", glow: "77, 163, 255",  label: "Awaiting" },
};

export const ProductivityRing = ({ value = 0, color = "#00E5A8", size = 52, stroke = 4, isDark = true }) => {
  const v = Math.max(0, Math.min(100, value));
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const trackStroke = isDark ? "rgba(255,255,255,0.08)" : "rgba(15,23,42,0.08)";
  const labelFill   = isDark ? "#FFFFFF" : "#0F172A";
  // Gradient ID needs to be unique per theme so the dark/light swatches
  // don't share a definition cached from the previous mount.
  const gradId = `pg-${color.replace("#","")}-${isDark ? "d" : "l"}`;
  return (
    <svg width={size} height={size} viewBox={`0 0 ${size} ${size}`} style={{ display: "block" }}>
      <defs>
        <linearGradient id={gradId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%"  stopColor={color} stopOpacity="0.9" />
          <stop offset="100%" stopColor={color} stopOpacity="0.55" />
        </linearGradient>
      </defs>
      <circle cx={size/2} cy={size/2} r={r}
              stroke={trackStroke} strokeWidth={stroke} fill="none" />
      <circle cx={size/2} cy={size/2} r={r}
              stroke={`url(#${gradId})`}
              strokeWidth={stroke} fill="none"
              strokeLinecap="round"
              strokeDasharray={c}
              strokeDashoffset={c - (c * v) / 100}
              transform={`rotate(-90 ${size/2} ${size/2})`}
              style={{ transition: "stroke-dashoffset .6s cubic-bezier(.4,1.6,.6,1)" }} />
      <text x="50%" y="50%" dy="0.35em" textAnchor="middle"
            fontSize="11" fontWeight="800" fill={labelFill}
            style={{ fontFamily: "inherit", letterSpacing: "-0.3px" }}>{v}%</text>
    </svg>
  );
};

export const PulseBar = ({ accent, active, isDark = true }) => (
  <div style={{
    position: "relative", height: 3, borderRadius: 3, overflow: "hidden",
    background: isDark ? "rgba(255,255,255,0.06)" : "rgba(15,23,42,0.08)",
    marginTop: 4,
  }}>
    {active && (
      <div style={{
        position: "absolute", inset: 0,
        background: `linear-gradient(90deg, transparent, ${accent}, transparent)`,
        animation: "zt-pulse-slide 2.2s linear infinite",
      }} />
    )}
  </div>
);

export const APP_CATEGORY = (name = "") => {
  const n = name.toLowerCase();
  if (/zoom|meet|teams|slack|discord|skype|webex/.test(n)) return { label: "productive", color: "#16A34A", bg: "#F0FDF4" };
  if (/vscode|code|visual studio|intellij|pycharm|eclipse|vim|notepad|sublime/.test(n)) return { label: "productive", color: "#16A34A", bg: "#F0FDF4" };
  if (/chrome|edge|firefox|safari|browser/.test(n)) return { label: "neutral", color: "#7C3AED", bg: "#F5F3FF" };
  // Business comms (WhatsApp, WhatsApp.Root, Signal, Messenger, Skype) are
  // routed through the neutral fallback below — they're commonly used for
  // work conversations, so the client-side fallback shouldn't default
  // them to "unproductive". Backend defaults match (see
  // backend/app/app_review/productivity.py).
  if (/\bwhatsapp(\.root|rt)?\b|\bsignal\b|\bmessenger\b|\bskype\b/.test(n)) return { label: "neutral", color: "#7C3AED", bg: "#F5F3FF" };
  if (/telegram|instagram|facebook|twitter|youtube|netflix|tiktok/.test(n)) return { label: "unproductive", color: "#DC2626", bg: "#FEF2F2" };
  if (/figma|photoshop|illustrator|canva|sketch/.test(n)) return { label: "productive", color: "#16A34A", bg: "#F0FDF4" };
  if (/excel|word|powerpoint|sheets|docs/.test(n)) return { label: "productive", color: "#16A34A", bg: "#F0FDF4" };
  return { label: "neutral", color: "#7C3AED", bg: "#F5F3FF" };
};

