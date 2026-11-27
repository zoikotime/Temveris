import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import TemverisLogoWhite from "../assets/Temveris_Logo_White.svg";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";
import { useWorkspace } from "../context/WorkspaceContext";
import { useEntitlements } from "../context/EntitlementsContext";
import {
  presentationForNav, shouldRenderNav, PRESENTATION,
} from "../entitlements/registry";

const ICON_PATHS = {
  dashboard:     "M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2z M9 22V12h6v10",
  users:         "M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2 M23 21v-2a4 4 0 00-3-3.87 M16 3.13a4 4 0 010 7.75",
  clock:         "M12 22c5.523 0 10-4.477 10-10S17.523 2 12 2 2 6.477 2 12s4.477 10 10 10z M12 6v6l4 2",
  camera:        "M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z M12 17a4 4 0 100-8 4 4 0 000 8z",
  bar_chart:     "M18 20V10 M12 20V4 M6 20v-6",
  briefcase:     "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  settings:      "M12 15a3 3 0 100-6 3 3 0 000 6z M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z",
  log_out:       "M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4 M16 17l5-5-5-5 M21 12H9",
  eye:           "M1 12S5 4 12 4s11 8 11 8-4 8-11 8S1 12 1 12z M12 15a3 3 0 100-6 3 3 0 000 6z",
  calendar:      "M3 4h18v18H3z M16 2v4 M8 2v4 M3 10h18",
  tag:           "M20.59 13.41l-7.17 7.17a2 2 0 01-2.83 0L2 12V2h10l8.59 8.59a2 2 0 010 2.82z M7 7h.01",
  activity:      "M22 12h-4l-3 9L9 3l-3 9H2",
  timelapse:     "M12 2a10 10 0 100 20A10 10 0 0012 2z M12 6v6l4 2",
  download:      "M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4 M7 10l5 5 5-5 M12 15V3",
  plus:          "M12 5v14 M5 12h14",
  monitor:       "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  dollar:        "M12 1v22 M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6",
  shield:        "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z",
  link:          "M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71 M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71",
  chevron_down:  "M6 9l6 6 6-6",
  external:      "M18 13v6a2 2 0 01-2 2H5a2 2 0 01-2-2V8a2 2 0 012-2h6 M15 3h6v6 M10 14L21 3",
};

const Icon = ({ n, s = 16, c = "#475467" }) => {
  return (
    <svg width={s} height={s} viewBox="0 0 24 24" fill="none"
      stroke={c} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
      style={{ display: "inline-block", flexShrink: 0 }}>
      {(ICON_PATHS[n] || "").split(" M").map((seg, i) => (
        <path key={i} d={i === 0 ? seg : "M" + seg} />
      ))}
    </svg>
  );
};

const initials = (name) =>
  (name || "").split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2) || "U";

const NAV_ITEMS = [
  { section: "OVERVIEW", items: [
    { k: "overview",     icon: "dashboard",  label: "Overview",                 route: "/admin" },
  ]},
  { section: "TEAM MANAGEMENT", items: [
    { k: "myteam",       icon: "users",      label: "Team Directory",           route: "/admin", tab: "myteam" },
    { k: "screenshots",  icon: "camera",     label: "Screenshots",              route: "/admin", tab: "screenshots" },
    { k: "timesheets",   icon: "clock",      label: "Attendance & Timesheets",  route: "/admin", tab: "timesheets" },
    { k: "timelapse",    icon: "timelapse",  label: "Time Playback",            route: "/admin", tab: "timelapse" },
    { k: "reports",      icon: "bar_chart",  label: "Reporting",                route: "/admin", tab: "reports" },
    { k: "offboarding",  icon: "shield",     label: "Offboarding",              route: "/admin/offboarding" },
  ]},
  { section: "PRODUCTIVITY", items: [
    { k: "activitylog",       icon: "activity", label: "Activity Log",          route: "/admin", tab: "activitylog" },
    { k: "appshistory",       icon: "activity", label: "Apps History",          route: "/admin", tab: "appshistory" },
    { k: "browsercategories", icon: "tag",      label: "Website Categories",    route: "/admin", tab: "browsercategories" },
  ]},
  { section: "LEAVE", items: [
    { k: "manageleave",   icon: "calendar",  label: "Leave Management", route: "/admin/leave" },
    { k: "leavesummary",  icon: "briefcase", label: "Leave Summary",    route: "/admin", tab: "leavesummary" },
    { k: "manageholiday", icon: "tag",       label: "Public Holidays",  route: "/admin", tab: "manageholiday" },
  ]},
  { section: "FINANCE", items: [
    { k: "payroll",      icon: "dollar",     label: "Payroll Calculator", route: "/admin", tab: "payroll" },
    { k: "billing",      icon: "briefcase",  label: "Billing",            route: "/admin", tab: "billing" },
  ]},
  { section: "SYSTEM", items: [
    { k: "settings",     icon: "settings",   label: "Workspace Settings", route: "/settings" },
  ]},
];

function AdminSidebar({ activeKey }) {
  const navigate = useNavigate();
  const location = useLocation();
  const { isDark } = useTheme();
  const { T } = useLanguage();
  const { user, logout } = useAuth();
  const { workspaceName, workspaceLogo } = useWorkspace();
  // Presentation projection from the backend (SPEC.txt §7). Presentation only —
  // the server remains the sole authority on access; a rendered item is never a
  // grant. `projection` is null until it loads or if the fetch failed, in which
  // case every item renders as today (safe: no server enforcement exists).
  const { projection } = useEntitlements();

  // Memoize label maps so they're not rebuilt on every re-render — the
  // strings only change when the active translation table (T) flips. With
  // React.memo wrapping the export, this means sidebar work is virtually
  // free between language switches.
  const sectionLabelMap = useMemo(() => ({
    OVERVIEW:          T.sectionOverview        || "Overview",
    "TEAM MANAGEMENT": T.sectionTeamManagement  || "Team Management",
    SECURITY:          T.sectionSecurity        || "Security",
    PRODUCTIVITY:      T.sectionProductivity    || "Productivity",
    LEAVE:             T.sectionLeave           || "Leave",
    FINANCE:           T.sectionFinance         || "Finance",
    SYSTEM:            T.sectionSystem          || "System",
  }), [T]);

  const itemLabelMap = useMemo(() => ({
    profile:           T.myProfile              || "My Profile",
    overview:          T.overviewSidebar        || "Overview",
    myteam:            T.teamDirectory          || "Team Directory",
    screenshots:       T.screenshotsSidebar     || "Screenshots",
    timesheets:        T.attendanceTimesheets   || "Attendance & Timesheets",
    timelapse:         T.timePlayback           || "Time Playback",
    reports:           T.reporting              || "Reporting",
    appshistory:       T.appsHistoryNav         || "Apps History",
    browsercategories: T.websiteCategories      || "Website Categories",
    manageleave:       T.leaveManagement        || "Leave Management",
    leavesummary:      T.leaveSummarySidebar    || "Leave Summary",
    manageholiday:     T.publicHolidays         || "Public Holidays",
    applyleave:        T.applyLeaveSidebar      || "Apply Leave",
    payroll:           T.payrollCalculator      || "Payroll Calculator",
    billing:           T.billingSidebar         || "Billing",
    downloadapp:       T.downloadAppSidebar     || "Download App",
    settings:          T.workspaceSettings      || "Workspace Settings",
  }), [T]);

  const [menuOpen, setMenuOpen] = useState(false);
  // The account menu closes on a click anywhere outside it, or Escape.
  const footerRef = useRef(null);
  useEffect(() => {
    if (!menuOpen) return undefined;
    const onDown = (e) => { if (footerRef.current && !footerRef.current.contains(e.target)) setMenuOpen(false); };
    const onKey = (e) => { if (e.key === "Escape") setMenuOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("mousedown", onDown); document.removeEventListener("keydown", onKey); };
  }, [menuOpen]);

  // Live user from AuthContext; fall back to localStorage on first paint.
  const adminUser = (() => {
    if (user) return { name: user.name || "Admin", email: user.email || "" };
    try {
      const name = localStorage.getItem("name") || localStorage.getItem("user_name") || "Admin";
      const email = localStorage.getItem("email") || localStorage.getItem("user_email") || "";
      return { name, email };
    } catch { return { name: "Admin", email: "" }; }
  })();

  // Determine which key is active based on route or explicit prop. For
  // /admin we honour the ?tab= query so the highlight tracks the visible
  // sub-tab instead of always showing "Overview".
  const getActive = () => {
    if (activeKey) return activeKey;
    const path = location.pathname;
    if (path === "/settings") return "settings";
    if (path === "/admin/leave") return "manageleave";
    if (path.startsWith("/admin/offboarding")) return "offboarding";
    if (path === "/leave") return "applyleave";
    if (path === "/account-settings") return "accountsettings";
    if (path === "/profile") return "profile";
    if (path === "/admin") {
      try {
        const t = new URLSearchParams(location.search).get("tab");
        if (t) return t;
      } catch {}
      return "overview";
    }
    return "overview";
  };

  const active = getActive();

  // Presentation affordance for a nav key: a small badge for LOCK (upgrade),
  // READ_ONLY (view-only) or CONFIGURATION_REQUIRED (setup — never an upsell).
  // Returns null for SHOW / unmapped / not-yet-loaded, so today's look is
  // unchanged for entitled items. Purely decorative; it changes nothing about
  // navigation, which is deliberately left working (no client-side blocking).
  const PresBadge = ({ navKey }) => {
    const p = presentationForNav(navKey, projection);
    if (p === PRESENTATION.LOCK) {
      return <span className="ztsb-badge-pres ztsb-badge-lock" title="Available on a higher plan">🔒</span>;
    }
    if (p === PRESENTATION.READ_ONLY) {
      return <span className="ztsb-badge-pres ztsb-badge-ro" title="Read-only">RO</span>;
    }
    if (p === PRESENTATION.CONFIGURATION_REQUIRED) {
      return <span className="ztsb-badge-pres ztsb-badge-cfg" title="Setup required">SET UP</span>;
    }
    return null;
  };

  const handleClick = (item) => {
    const currentTab = new URLSearchParams(location.search).get("tab");
    if (item.tab) {
      // No-op only when we're already on the exact same tab.
      if (location.pathname === item.route && currentTab === item.tab) return;
      navigate(`/admin?tab=${item.tab}`);
    } else if (item.k === "overview") {
      // Always navigate to ?tab=overview so there is always a URL change.
      // navigate("/admin") is a no-op when the URL is already /admin (no tab),
      // which means location.search never changes and the sync useEffect in
      // AdminDashboard never fires — leaving the stale tab state in place.
      if (currentTab === "overview" || (!currentTab && location.pathname === "/admin")) return;
      navigate("/admin?tab=overview");
    } else {
      if (location.pathname === item.route && !currentTab) return;
      navigate(item.route);
    }
  };

  // uiverse-style click ripple — appends a short-lived span at the cursor.
  const spawnRipple = (e) => {
    try {
      const btn = e.currentTarget;
      if (!btn) return;
      const rect = btn.getBoundingClientRect();
      const span = document.createElement("span");
      span.className = "ztsb-ripple";
      span.style.left = (e.clientX - rect.left - 7) + "px";
      span.style.top  = (e.clientY - rect.top  - 7) + "px";
      btn.appendChild(span);
      setTimeout(() => span.remove(), 600);
    } catch { /* no-op */ }
  };

  // Navy shell (Live Workforce Overview design). Fixed colours rather than
  // theme tokens: the sidebar is dark in both themes, a shade deeper in dark
  // mode so it still separates from the near-black page.
  const S = {
    bg:       isDark ? "linear-gradient(180deg, #0B1828 0%, #070F1B 100%)" : "linear-gradient(180deg, #0F2846 0%, #0A1D35 100%)",
    text:     "#E6EDF5",
    sub:      "#A9B9CC",
    section:  "#8FA3BC",
    icon:     "#C9D5E3",
    hover:    "rgba(255,255,255,.08)",
    divider:  "rgba(255,255,255,.10)",
    active:   "linear-gradient(135deg, #2DB36A 0%, #1E9254 100%)",
    activeGlow: "0 6px 16px -8px rgba(45,179,106,.75)",
  };

  const itemStyle = (on, child = false) => ({
    position: "relative", overflow: "hidden",
    width: "100%", display: "flex", alignItems: "center", gap: child ? 9 : 12,
    padding: child ? "8px 12px" : "10px 12px", borderRadius: 10, border: "none",
    background: on ? S.active : "transparent",
    boxShadow: on ? S.activeGlow : "none",
    color: on ? "#FFFFFF" : S.text,
    fontWeight: on ? 700 : 500, fontSize: child ? 13 : 14,
    cursor: "pointer", fontFamily: "inherit", marginBottom: 2, textAlign: "left",
    transition: "background .18s ease, color .18s ease, transform .18s cubic-bezier(.34,1.56,.64,1)",
  });

  return (
    <>
    <aside data-testid="admin-sidebar" className="ztsb-scroll" style={{ width: 236, flexShrink: 0, background: S.bg, display: "flex", flexDirection: "column", height: "100vh", overflowY: "auto", color: S.text }}>
      <style>{`
        @keyframes ztsbSheen {
          0%   { transform: translateX(-120%) skewX(-18deg); opacity: 0; }
          40%  { opacity: 1; }
          100% { transform: translateX(220%)  skewX(-18deg); opacity: 0; }
        }
        .ztsb-btn { isolation: isolate; }
        .ztsb-btn::before {
          content:""; position:absolute; inset:0;
          background: linear-gradient(120deg, transparent 0%, rgba(255,255,255,.10) 50%, transparent 100%);
          transform: translateX(-120%) skewX(-18deg); pointer-events:none; opacity:0; z-index:0;
        }
        .ztsb-btn:hover::before { animation: ztsbSheen .65s ease forwards; }
        .ztsb-btn[data-active="false"]:hover { background: ${S.hover} !important; color: #FFFFFF !important; transform: translateX(2px); }
        .ztsb-btn[data-active="false"]:hover svg { stroke: #FFFFFF !important; }
        .ztsb-btn[data-active="true"]:hover { transform: translateX(2px); }
        .ztsb-btn:active { transform: translateX(2px) scale(.985); }
        .ztsb-btn:focus-visible { outline: 2px solid #7CF0AE; outline-offset: 1px; }
        .ztsb-btn:hover .ztsb-icon { transform: rotate(-6deg) scale(1.12); }
        .ztsb-btn > * { position: relative; z-index: 1; }
        .ztsb-btn svg { transition: stroke .18s ease, background .18s ease, transform .2s ease; }
        /* Presentation badges (Phase 3.7) — decorative only, no interaction. */
        .ztsb-badge-pres {
          display: inline-flex; align-items: center; justify-content: center;
          font-size: 8px; font-weight: 800; letter-spacing: .3px;
          padding: 1px 6px; border-radius: 999px; margin-left: 4px;
          white-space: nowrap; line-height: 1.4;
        }
        .ztsb-badge-lock { background: transparent; font-size: 11px; padding: 0; }
        .ztsb-badge-ro  { background: rgba(255,255,255,.14); color: #FFFFFF; }
        .ztsb-badge-cfg { background: #FEF3C7; color: #92400E; }
        /* Ripple */
        @keyframes ztsbRipple { to { transform: scale(2.6); opacity: 0; } }
        .ztsb-ripple {
          position: absolute; border-radius: 50%;
          background: rgba(255,255,255,.28); pointer-events: none;
          width: 14px; height: 14px; transform: scale(0); opacity: 1;
          animation: ztsbRipple .55s ease-out forwards; z-index: 0;
        }
        .ztsb-cta { transition: background .18s ease, transform .18s ease, border-color .18s ease; }
        .ztsb-cta:hover { background: rgba(255,255,255,.08) !important; border-color: #FFFFFF !important; transform: translateY(-1px); }
        .ztsb-cta:focus-visible { outline: 2px solid #7CF0AE; outline-offset: 2px; }
        .ztsb-menu-item:hover { background: rgba(255,255,255,.08) !important; }
        .ztsb-user:hover { background: rgba(255,255,255,.06) !important; }
        .ztsb-user:focus-visible { outline: 2px solid #7CF0AE; outline-offset: 1px; }
        /* Thin, translucent scrollbar on the navy instead of the app-wide
           wide brand-blue one (index.css), which read as a bright stripe here. */
        .ztsb-scroll { scrollbar-width: thin; scrollbar-color: rgba(255,255,255,.22) transparent; }
        .ztsb-scroll::-webkit-scrollbar { width: 6px !important; height: 6px !important; }
        .ztsb-scroll::-webkit-scrollbar-track { background: transparent !important; }
        .ztsb-scroll::-webkit-scrollbar-thumb { background: rgba(255,255,255,.22) !important; border: none !important; border-radius: 6px !important; }
        .ztsb-scroll::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,.38) !important; }
      `}</style>
      <div style={{ padding: "20px 16px 14px", display: "flex", flexDirection: "column", alignItems: "center", gap: 6, flexShrink: 0 }}>
        {workspaceLogo ? (
          // An uploaded logo may be dark-on-transparent, so it sits on a white
          // tile rather than straight on the navy.
          <span style={{ background: "#FFFFFF", borderRadius: 10, padding: "6px 10px", display: "inline-flex", maxWidth: "100%", boxSizing: "border-box" }}>
            <img
              src={workspaceLogo}
              alt={workspaceName || "Workspace"}
              style={{ maxWidth: 176, height: "auto", maxHeight: 34, objectFit: "contain", display: "block", borderRadius: 4 }}
            />
          </span>
        ) : (
          <img
            src={TemverisLogoWhite}
            alt="Temveris"
            onClick={() => navigate("/admin?tab=overview")}
            style={{ width: "100%", maxWidth: 190, height: "auto", display: "block", cursor: "pointer" }}
          />
        )}
        <p style={{ fontSize: 8, color: S.sub, fontWeight: 700, letterSpacing: 0.3, margin: 0, textAlign: "center", lineHeight: 1.2, textTransform: "uppercase", whiteSpace: "nowrap" }}>
          {T.brandTagline || "Workforce Truth • Financial Control"}
        </p>
        {workspaceName && (
          <p style={{ fontSize: 13, color: S.text, fontWeight: 600, margin: "2px 0 0", textAlign: "center", lineHeight: 1.25 }}>
            {workspaceName}
          </p>
        )}
      </div>
      <nav className="ztsb-scroll" style={{ flex: 1, padding: "4px 12px 8px", overflowY: "auto" }}>
        {/* ── Open My Dashboard CTA — admins can jump to their personal
             worker dashboard from anywhere in the admin surface. ── */}
        <button
          className="ztsb-cta"
          onClick={() => navigate("/dashboard")}
          style={{
            width: "100%", margin: "4px 0 12px", padding: "10px 12px",
            borderRadius: 10, border: "1.5px solid rgba(255,255,255,.55)",
            background: "transparent",
            color: "#FFFFFF", fontWeight: 700, fontSize: 13.5, cursor: "pointer",
            fontFamily: "inherit", textAlign: "center", letterSpacing: ".2px",
            display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
          }}
        >
          {T.openMyDashboard || "Open My Dashboard"}
          <Icon n="external" s={14} c="#FFFFFF" />
        </button>

        {NAV_ITEMS.map(({ section, items }) => (
          <div key={section} style={{ marginBottom: 6 }}>
            <p style={{ fontSize: 11, fontWeight: 700, color: S.section, letterSpacing: 1.6, padding: "12px 12px 6px", margin: 0, textTransform: "uppercase" }}>{sectionLabelMap[section] || section}</p>
            {items.map((item) => {
              // HIDE removes the item entirely (spec §7). Unmapped items and the
              // load/error fallback keep rendering — see registry.shouldRenderNav.
              if (!shouldRenderNav(item.k, projection)) return null;
              const isA = active === item.k;
              return (
                <button key={item.k}
                  className="ztsb-btn"
                  data-active={isA ? "true" : "false"}
                  aria-current={isA ? "page" : undefined}
                  onClick={(e) => { spawnRipple(e); handleClick(item); }}
                  style={itemStyle(isA)}>
                  <span className="ztsb-icon" style={{ display: "inline-flex", transition: "transform .25s cubic-bezier(.34,1.56,.64,1)" }}>
                    <Icon n={item.icon} s={17} c={isA ? "#FFFFFF" : S.icon} />
                  </span>
                  <span style={{ flex: 1 }}>{itemLabelMap[item.k] || item.label}</span>
                  <PresBadge navKey={item.k} />
                </button>
              );
            })}
          </div>
        ))}
      </nav>
      <div ref={footerRef} style={{ padding: "12px", borderTop: `1px solid ${S.divider}`, flexShrink: 0, position: "relative" }}>
        {menuOpen && (
          <div role="menu" style={{
            position: "absolute", left: 12, right: 12, bottom: "calc(100% - 4px)", zIndex: 5,
            background: "#132F52", border: `1px solid ${S.divider}`, borderRadius: 10, padding: 6,
            boxShadow: "0 -12px 28px rgba(0,0,0,.35)", display: "flex", flexDirection: "column",
          }}>
            {[
              { k: "profile", label: T.myProfile || "My Profile",             icon: "users",    onClick: () => navigate("/profile") },
              { k: "account", label: T.accountSettings || "Account Settings", icon: "settings", onClick: () => navigate("/account-settings") },
              { k: "signout", label: T.signOut || "Sign Out",                 icon: "log_out",  danger: true,
                onClick: () => { logout({ redirect: false }); navigate("/", { replace: true }); } },
            ].map((m) => (
              <button key={m.k} type="button" role="menuitem" className="ztsb-menu-item"
                onClick={() => { setMenuOpen(false); m.onClick(); }}
                style={{ display: "flex", alignItems: "center", gap: 9, padding: "9px 10px", borderRadius: 8, border: "none", background: "transparent", color: m.danger ? "#FCA5A5" : S.text, fontWeight: 600, fontSize: 13, cursor: "pointer", fontFamily: "inherit", textAlign: "left" }}>
                <Icon n={m.icon} s={14} c={m.danger ? "#FCA5A5" : S.icon} />{m.label}
              </button>
            ))}
          </div>
        )}
        <button type="button" className="ztsb-user" aria-haspopup="menu" aria-expanded={menuOpen}
          aria-label={T.accountMenu || "Account menu"}
          onClick={() => setMenuOpen(o => !o)}
          style={{ width: "100%", display: "flex", alignItems: "center", gap: 10, padding: "8px 6px", borderRadius: 10, border: "none", background: "transparent", cursor: "pointer", fontFamily: "inherit", textAlign: "left" }}>
          <span style={{ width: 36, height: 36, borderRadius: "50%", flexShrink: 0, display: "flex", alignItems: "center", justifyContent: "center", color: "#fff", fontWeight: 800, fontSize: 13, background: "linear-gradient(135deg, #2DB36A, #1E9254)" }}>{initials(adminUser.name)}</span>
          <span style={{ overflow: "hidden", flex: 1, minWidth: 0 }}>
            <span style={{ display: "block", fontWeight: 700, fontSize: 13, color: "#FFFFFF", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{adminUser.name}</span>
            <span style={{ display: "block", fontSize: 11.5, color: S.sub, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{adminUser.email}</span>
          </span>
          <span style={{ display: "inline-flex", transition: "transform .18s ease", transform: menuOpen ? "rotate(180deg)" : "none" }}>
            <Icon n="chevron_down" s={15} c={S.sub} />
          </span>
        </button>
      </div>
    </aside>
    </>
  );
}

// Memoized so the sidebar doesn't re-render on every parent state update
// (AdminDashboard alone fires ~5-10 re-renders per minute from polling /
// WebSocket / search input). Theme, language and auth changes still flow
// through their context hooks — only equal-prop renders are skipped.
export default React.memo(AdminSidebar);
