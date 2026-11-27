/**
 * Super-Admin Portal — platform-ops view of every commercial tenant.
 * Endpoints used:
 *   GET  /api/super-admin/stats
 *   GET  /api/super-admin/organizations
 *   GET  /api/super-admin/organizations/{id}
 *   POST /api/super-admin/organizations/{id}/suspend
 *   POST /api/super-admin/organizations/{id}/restore
 *   GET  /api/super-admin/signups
 *   GET  /api/super-admin/audit
 *   POST /api/super-admin/impersonate
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { API_BASE } from "../config";
import { useTheme } from "../context/ThemeContext";
import { writeToken, mirrorUserToStorage, clearAuthStorage } from "../api/authStorage";
import { useAuth } from "../context/AuthContext";
import TemverisLogo from "../assets/Temveris_Logo.svg";
import TemverisLogoWhite from "../assets/Temveris_Logo_White.svg";

const TOKEN = () => localStorage.getItem("zt_token") || localStorage.getItem("token") || "";

const sa = async (path, opts = {}) => {
  const res = await fetch(`${API_BASE}/api/super-admin${path}`, {
    headers: {
      Authorization: `Bearer ${TOKEN()}`,
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
    ...opts,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    // FastAPI `detail` is a bare string on simple errors and a {code, message}
    // object on the structured lifecycle ones. Without this the object path
    // renders as "[object Object]" in the toast.
    const detail = body.detail;
    const message =
      typeof detail === "string" ? detail : detail?.message;
    const err = new Error(message || `HTTP ${res.status}`);
    err.status = res.status;
    err.code = typeof detail === "object" ? detail?.code : undefined;
    throw err;
  }
  return res.json();
};

// ── Design tokens ──────────────────────────────────────────────────────────────
const tokens = (isDark) => isDark ? {
  bg: "#0A0A0A",
  bgGrad: "radial-gradient(1200px 700px at 80% -10%, #161616, #0A0A0A) fixed",
  panel: "#141414", panel2: "#1A1A1A", panel3: "#222222",
  border: "rgba(255,255,255,.07)", borderStrong: "rgba(255,255,255,.13)",
  txt: "#EDEDED", txtMute: "#9A9A9A", txtDim: "#6A6A6A",
  blue: "#3D6BFF", blueSoft: "rgba(61,107,255,.14)",
  teal: "#00BFA9", tealSoft: "rgba(0,191,169,.13)",
  ok: "#34C98A", okSoft: "rgba(52,201,138,.13)",
  warn: "#EBA73B", warnSoft: "rgba(235,167,59,.13)",
  bad: "#F0594F", badSoft: "rgba(240,89,79,.13)",
  rowHover: "rgba(255,255,255,.035)",
  shadow: "0 18px 48px -12px rgba(0,0,0,.65)",
  navy: "#242424",
  overlayBg: "rgba(0,0,0,.62)",
} : {
  // Neutral enterprise palette (Stripe / Linear / Vercel style): white cards on
  // a near-white canvas, hairline borders, indigo accent. Colour is reserved for
  // icons, badges, and bars — never full-card fills.
  bg: "#F8FAFC",
  bgGrad: "#F8FAFC",
  panel: "#FFFFFF", panel2: "#FFFFFF", panel3: "#F3F4F6",
  border: "#E5E7EB", borderStrong: "#D1D5DB",
  txt: "#111827", txtMute: "#6B7280", txtDim: "#9CA3AF",
  blue: "#4F46E5", blueSoft: "rgba(79,70,229,.10)",
  teal: "#0D9488", tealSoft: "rgba(13,148,136,.10)",
  ok: "#059669", okSoft: "rgba(5,150,105,.10)",
  warn: "#D97706", warnSoft: "rgba(217,119,6,.12)",
  bad: "#DC2626", badSoft: "rgba(220,38,38,.10)",
  rowHover: "rgba(17,24,39,.028)",
  shadow: "0 1px 3px rgba(16,24,40,.06), 0 1px 2px rgba(16,24,40,.04)",
  navy: "#111827",
  overlayBg: "rgba(6,11,22,.45)",
};

// ── Helpers ───────────────────────────────────────────────────────────────────
const TIMEZONES = [
  "Asia/Kolkata","Asia/Dubai","Asia/Singapore","Asia/Tokyo","Asia/Shanghai",
  "Asia/Seoul","Asia/Jakarta","Asia/Kuala_Lumpur","Asia/Bangkok","Asia/Colombo",
  "Asia/Dhaka","Asia/Karachi","Asia/Riyadh","Asia/Istanbul",
  "Europe/London","Europe/Paris","Europe/Berlin","Europe/Amsterdam","Europe/Zurich",
  "Europe/Moscow","Europe/Warsaw","Europe/Stockholm","Europe/Rome","Europe/Madrid",
  "America/New_York","America/Chicago","America/Denver","America/Los_Angeles",
  "America/Toronto","America/Sao_Paulo","America/Mexico_City","America/Buenos_Aires",
  "Africa/Nairobi","Africa/Lagos","Africa/Johannesburg","Africa/Cairo",
  "Australia/Sydney","Australia/Melbourne","Pacific/Auckland","UTC",
];
const CURRENCIES = [
  "USD","EUR","GBP","INR","AED","SGD","JPY","CNY","AUD","CAD",
  "CHF","HKD","MYR","THB","IDR","PKR","BDT","LKR","NGN","KES",
  "ZAR","EGP","SAR","BRL","MXN","ARS","PLN","SEK","NOK","DKK",
];
const COUNTRIES = [
  { code: "IN", name: "India" },
  { code: "US", name: "United States" },
  { code: "GB", name: "United Kingdom" },
  { code: "AE", name: "United Arab Emirates" },
  { code: "SG", name: "Singapore" },
  { code: "AU", name: "Australia" },
  { code: "CA", name: "Canada" },
  { code: "DE", name: "Germany" },
  { code: "FR", name: "France" },
  { code: "NL", name: "Netherlands" },
  { code: "CH", name: "Switzerland" },
  { code: "SE", name: "Sweden" },
  { code: "NO", name: "Norway" },
  { code: "DK", name: "Denmark" },
  { code: "PL", name: "Poland" },
  { code: "IT", name: "Italy" },
  { code: "ES", name: "Spain" },
  { code: "JP", name: "Japan" },
  { code: "CN", name: "China" },
  { code: "KR", name: "South Korea" },
  { code: "ID", name: "Indonesia" },
  { code: "MY", name: "Malaysia" },
  { code: "TH", name: "Thailand" },
  { code: "PH", name: "Philippines" },
  { code: "VN", name: "Vietnam" },
  { code: "PK", name: "Pakistan" },
  { code: "BD", name: "Bangladesh" },
  { code: "LK", name: "Sri Lanka" },
  { code: "SA", name: "Saudi Arabia" },
  { code: "QA", name: "Qatar" },
  { code: "KW", name: "Kuwait" },
  { code: "BH", name: "Bahrain" },
  { code: "OM", name: "Oman" },
  { code: "NG", name: "Nigeria" },
  { code: "KE", name: "Kenya" },
  { code: "ZA", name: "South Africa" },
  { code: "EG", name: "Egypt" },
  { code: "BR", name: "Brazil" },
  { code: "MX", name: "Mexico" },
  { code: "AR", name: "Argentina" },
  { code: "HK", name: "Hong Kong" },
  { code: "NZ", name: "New Zealand" },
  { code: "RU", name: "Russia" },
  { code: "TR", name: "Turkey" },
];

function slugify(s) {
  return (s || "").toLowerCase().replace(/[^a-z0-9\s-]/g,"").trim()
    .replace(/[\s]+/g,"-").replace(/-+/g,"-").slice(0,48);
}

const STATE_MAP = {
  active:       { label: "Active",        cls: "ok"   },
  trial_active: { label: "Trial",         cls: "blue" },
  trial_expired:{ label: "Trial Expired", cls: "warn" },
  past_due:     { label: "Past Due",      cls: "warn" },
  suspended:    { label: "Suspended",     cls: "bad"  },
  cancelled:    { label: "Cancelled",     cls: "mute" },
};

function healthFromOrg(o) {
  const state = (o.commercial_state || "").toLowerCase();
  if (["suspended","cancelled"].includes(state)) return null;

  // Billing Risk — 25 pts
  let billing = 0;
  if (state === "active") billing = 25;
  else if (state.includes("trial")) billing = 15;
  else if (state === "past_due") billing = 5;

  // Usage — 20 pts (based on last_active recency)
  const lastDays = o.last_active
    ? Math.floor((Date.now() - new Date(o.last_active).getTime()) / 86400000)
    : 999;
  let usage = 0;
  if (lastDays <= 1) usage = 20;
  else if (lastDays <= 7) usage = 14;
  else if (lastDays <= 14) usage = 8;
  else if (lastDays <= 30) usage = 4;

  // Security 20, Compliance 15, Support 10, Integration 10 — full score (no negative signals yet)
  return Math.min(billing + usage + 20 + 15 + 10 + 10, 100);
}

function healthBand(s) {
  if (s === null) return "na";
  if (s >= 90) return "healthy";
  if (s >= 70) return "watch";
  if (s >= 40) return "atrisk";
  return "critical";
}

const BAND = {
  healthy:  { label: "Healthy",  color: "#34C98A" },
  watch:    { label: "Watch",    color: "#EBA73B" },
  atrisk:   { label: "At Risk",  color: "#F0594F" },
  critical: { label: "Critical", color: "#D63B3B" },
  na:       { label: "N/A",      color: "#8392B0" },
};

// ── Small UI components ───────────────────────────────────────────────────────
function Badge({ cls, label, D }) {
  const styles = {
    ok:   { bg: D.okSoft,   fg: D.ok   },
    blue: { bg: D.blueSoft, fg: D.blue },
    teal: { bg: D.tealSoft, fg: D.teal },
    warn: { bg: D.warnSoft, fg: D.warn },
    bad:  { bg: D.badSoft,  fg: D.bad  },
    mute: { bg: D.panel3,   fg: D.txtMute, border: `1px solid ${D.border}` },
  };
  const s = styles[cls] || styles.mute;
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 6,
      padding: "4px 11px", borderRadius: 999,
      fontSize: 11, fontWeight: 600, letterSpacing: .2, whiteSpace: "nowrap",
      background: s.bg, color: s.fg, border: s.border || "none",
    }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: s.fg, flexShrink: 0 }} />
      {label}
    </span>
  );
}

function StatePillNew({ state, D }) {
  const m = STATE_MAP[(state || "").toLowerCase()] || { label: state || "—", cls: "mute" };
  return <Badge cls={m.cls} label={m.label} D={D} />;
}

function HealthRing({ score, D }) {
  if (score === null) return <Badge cls="mute" label="N/A" D={D} />;
  const band = healthBand(score);
  const meta = BAND[band];
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, minWidth: 112 }}>
      <div style={{ display: "flex", alignItems: "baseline", gap: 7 }}>
        <span style={{ fontSize: 13, fontWeight: 700, color: D.txt, fontVariantNumeric: "tabular-nums" }}>{score}%</span>
        <span style={{ fontSize: 11, fontWeight: 600, color: meta.color }}>{meta.label}</span>
      </div>
      <div style={{ height: 5, borderRadius: 3, background: D.panel3, overflow: "hidden", width: "100%" }}>
        <div style={{ height: "100%", borderRadius: 3, background: meta.color, width: `${Math.max(0, Math.min(100, score))}%`, transition: "width .3s ease" }} />
      </div>
    </div>
  );
}

function SeatsBar({ used, limit, D }) {
  const pct = limit > 0 ? Math.round(used / limit * 100) : 0;
  const color = pct >= 90 ? D.warn : pct >= 70 ? D.blue : D.teal;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 64 }}>
      <span style={{ fontSize: 10.5, fontFamily: "inherit" }}>{used} / {limit}</span>
      <div style={{ height: 4, borderRadius: 3, background: D.panel3, overflow: "hidden" }}>
        <div style={{ height: "100%", borderRadius: 3, background: color, width: `${Math.min(pct, 100)}%` }} />
      </div>
    </div>
  );
}

function Btn({ children, variant = "ghost", onClick, disabled, style: s, D }) {
  const base = {
    height: 34, padding: "0 13px", borderRadius: 9, fontSize: 12, fontWeight: 600,
    display: "inline-flex", alignItems: "center", gap: 6, cursor: disabled ? "not-allowed" : "pointer",
    opacity: disabled ? .5 : 1, fontFamily: "inherit", transition: ".15s", ...s,
  };
  if (variant === "primary") return (
    <button onClick={onClick} disabled={disabled} style={{ ...base, background: D.blue, border: `1px solid ${D.blue}`, color: "#fff" }}>{children}</button>
  );
  if (variant === "danger") return (
    <button onClick={onClick} disabled={disabled} style={{ ...base, background: D.badSoft, border: `1px solid rgba(240,89,79,.3)`, color: D.bad }}>{children}</button>
  );
  return (
    <button onClick={onClick} disabled={disabled} style={{ ...base, background: D.panel3, border: `1px solid ${D.border}`, color: D.txt }}>{children}</button>
  );
}

// ── Line icon set (stroke = currentColor, sized inline) ─────────────────────────
const svg = (s, kids) => (
  <svg width={s} height={s} viewBox="0 0 24 24" fill="none" stroke="currentColor"
    strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">{kids}</svg>
);
const Icon = {
  building: (s = 16) => svg(s, <><path d="M3 21h18"/><path d="M5 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16"/><path d="M19 21V9a2 2 0 0 0-2-2h-2"/><path d="M9 7h2M9 11h2M9 15h2"/></>),
  activity: (s = 16) => svg(s, <path d="M22 12h-4l-3 9L9 3l-3 9H2"/>),
  alert:    (s = 16) => svg(s, <><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4"/><path d="M12 17h.01"/></>),
  rocket:   (s = 16) => svg(s, <><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="M12 15l-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></>),
  ban:      (s = 16) => svg(s, <><circle cx="12" cy="12" r="10"/><path d="m4.9 4.9 14.2 14.2"/></>),
  users:    (s = 16) => svg(s, <><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></>),
  layers:   (s = 16) => svg(s, <><path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z"/><path d="m22 17.65-9.17 4.16a2 2 0 0 1-1.66 0L2 17.65"/><path d="m22 12.65-9.17 4.16a2 2 0 0 1-1.66 0L2 12.65"/></>),
  shield:   (s = 16) => svg(s, <><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/></>),
  globe:    (s = 16) => svg(s, <><circle cx="12" cy="12" r="10"/><path d="M2 12h20"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></>),
  zap:      (s = 16) => svg(s, <path d="M13 2 3 14h9l-1 8 10-12h-9l1-8z"/>),
};

// ── Count-up — animates 0 → target once whenever the (numeric) value lands ──────
function useCountUp(target, active) {
  const [n, setN] = useState(0);
  const raf = useRef(0);
  useEffect(() => {
    if (!active || typeof target !== "number" || !isFinite(target)) return;
    let live = true;
    const dur = 700, start = performance.now();
    const tick = (now) => {
      if (!live) return;
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - Math.pow(1 - t, 3);   // easeOutCubic
      setN(Math.round(target * eased));
      if (t < 1) raf.current = requestAnimationFrame(tick);
    };
    raf.current = requestAnimationFrame(tick);
    return () => { live = false; cancelAnimationFrame(raf.current); };
  }, [target, active]);
  return n;
}

// ── KPI Card ──────────────────────────────────────────────────────────────────
function KpiCard({ label, value, loading, delta, dir, accent, cta, icon, onClick, D }) {
  const [hov, setHov] = useState(false);
  const ac = accent || D.blue;
  const numeric = typeof value === "number" && isFinite(value);
  const count = useCountUp(numeric ? value : 0, !loading && numeric);
  const dirColor = dir === "up" ? D.ok : dir === "down" ? D.bad : D.txtMute;
  return (
    <div onClick={onClick}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        // Subtle accent tint (~7%) over the canvas — soft SaaS colour, not a fill.
        background: ac + "12",
        border: `1px solid ${hov ? ac + "3D" : ac + "26"}`,
        borderRadius: 16, padding: 20, cursor: onClick ? "pointer" : "default",
        transform: hov ? "translateY(-3px)" : "none",
        boxShadow: hov ? "0 14px 30px -12px rgba(16,24,40,.16)" : D.shadow,
        transition: "transform .2s cubic-bezier(.2,.8,.2,1), box-shadow .2s, border-color .2s",
        minHeight: 138, display: "flex", flexDirection: "column",
      }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
        <div style={{ fontSize: 12.5, color: D.txtMute, fontWeight: 500 }}>{label}</div>
        <div style={{
          width: 38, height: 38, borderRadius: "50%", flexShrink: 0, display: "grid", placeItems: "center",
          color: ac, background: ac + "24",
        }}>{icon}</div>
      </div>

      <div style={{ fontSize: 32, fontWeight: 700, margin: "12px 0 6px", color: D.txt, fontVariantNumeric: "tabular-nums lining-nums", lineHeight: 1.1 }}>
        {loading ? "—" : numeric ? count.toLocaleString() : value}
      </div>

      <div style={{ fontSize: 12, display: "flex", alignItems: "center", gap: 5, color: D.txtMute }}>
        <span style={{ color: dirColor, fontWeight: 600 }}>
          {dir === "up" ? "↑" : dir === "down" ? "↓" : "→"} {delta}
        </span> vs last 7d
      </div>

      {cta && (
        <div style={{ fontSize: 12, color: ac, marginTop: "auto", paddingTop: 10, fontWeight: 600, display: "flex", alignItems: "center", gap: 4 }}>
          {cta} <span style={{ transform: hov ? "translateX(3px)" : "none", transition: "transform .2s" }}>→</span>
        </div>
      )}
    </div>
  );
}

// ── Mini KPI ──────────────────────────────────────────────────────────────────
function MiniKpi({ label, value, sub, loading, accent, icon, D }) {
  const [hov, setHov] = useState(false);
  const ac = accent || D.blue;
  const numeric = typeof value === "number" && isFinite(value);
  const count = useCountUp(numeric ? value : 0, !loading && numeric);
  return (
    <div
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        background: ac + "0F", border: `1px solid ${hov ? ac + "33" : ac + "1F"}`,
        borderRadius: 14, padding: "14px 15px", cursor: "default",
        transform: hov ? "translateY(-2px)" : "none",
        boxShadow: hov ? "0 8px 18px -8px rgba(16,24,40,.12)" : D.shadow,
        transition: "transform .16s, box-shadow .16s, border-color .16s",
      }}>
      <div style={{ display: "flex", alignItems: "center", gap: 9 }}>
        <span style={{ width: 30, height: 30, borderRadius: "50%", display: "grid", placeItems: "center", flexShrink: 0, color: ac, background: ac + "24" }}>{icon}</span>
        <span style={{ fontSize: 11.5, color: D.txtMute, fontWeight: 500 }}>{label}</span>
      </div>
      <div style={{ fontSize: 22, fontWeight: 700, marginTop: 8, fontVariantNumeric: "tabular-nums lining-nums", color: D.txt }}>
        {loading ? "—" : numeric ? count.toLocaleString() : value} <small style={{ fontSize: 11, color: D.txtDim, fontWeight: 500 }}>{sub}</small>
      </div>
    </div>
  );
}

function SavedViewBtn({ label, count, active, accent, onClick, D }) {
  const [hov, setHov] = useState(false);
  const ac = accent || D.blue;
  return (
    <button onClick={onClick}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        padding: "5px 11px", borderRadius: 20, fontSize: 11, fontWeight: 600,
        background: active ? ac : hov ? ac + "33" : D.panel3,
        border: `2px solid ${active || hov ? ac : D.border}`,
        color: active ? "#fff" : hov ? ac : D.txtMute,
        cursor: "pointer", transition: ".14s",
        transform: hov && !active ? "translateY(-1px)" : "none",
        fontWeight: hov || active ? 700 : 600,
      }}>
      {label}{count != null ? <span style={{ opacity: .7, marginLeft: 5, fontWeight: 500 }}>{count}</span> : null}
    </button>
  );
}

function TabBtn({ t, active, onClick, D }) {
  const [hov, setHov] = useState(false);
  return (
    <button onClick={onClick}
      onMouseEnter={() => setHov(true)} onMouseLeave={() => setHov(false)}
      style={{
        padding: "10px 15px", fontSize: 12.5, fontWeight: active ? 700 : 600,
        color: active ? D.txt : hov ? D.txt : D.txtMute,
        background: hov && !active ? D.blueSoft : "none",
        border: "none", borderBottom: `2px solid ${active ? D.blue : hov ? D.blue + "88" : "transparent"}`,
        borderRadius: "8px 8px 0 0",
        cursor: "pointer", display: "flex", alignItems: "center", gap: 7,
        fontFamily: "inherit", transition: ".15s",
      }}>
      {t.label}
      {t.count != null && (
        <span style={{
          fontSize: 10, padding: "1px 6px", borderRadius: 10,
          background: active ? D.blueSoft : hov ? D.blueSoft : D.panel3,
          border: `1px solid ${active || hov ? "transparent" : D.border}`,
          color: active || hov ? D.blue : D.txtMute, fontWeight: 600,
        }}>{t.count}</span>
      )}
    </button>
  );
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function Modal({ title, icon, iconCls, onClose, children, footer, D }) {
  const iconBg = iconCls === "warn" ? D.badSoft : D.blueSoft;
  const iconFg = iconCls === "warn" ? D.bad : D.blue;
  return (
    <div onClick={onClose} style={{
      position: "fixed", inset: 0, zIndex: 90,
      background: "rgba(6,11,22,.7)", backdropFilter: "blur(3px)",
      display: "grid", placeItems: "center", padding: 20,
    }}>
      <div onClick={e => e.stopPropagation()} style={{
        background: `linear-gradient(180deg,${D.panel2},${D.panel})`,
        border: `1px solid ${D.borderStrong}`, borderRadius: 14,
        width: 480, maxWidth: "100%",
        boxShadow: D.shadow,
        overflow: "hidden",
      }}>
        <div style={{ padding: "17px 19px 13px", display: "flex", gap: 12, alignItems: "flex-start" }}>
          {icon && (
            <div style={{
              width: 38, height: 38, borderRadius: 10, display: "grid", placeItems: "center",
              background: iconBg, color: iconFg, fontSize: 18, fontWeight: 700, flexShrink: 0,
            }}>{icon}</div>
          )}
          <div style={{ flex: 1 }}>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: D.txt }}>{title}</h3>
          </div>
          <button onClick={onClose} style={{ background: "none", border: "none", color: D.txtMute, cursor: "pointer", fontSize: 20, lineHeight: 1 }}>×</button>
        </div>
        <div style={{ padding: "0 19px 17px" }}>{children}</div>
        {footer && (
          <div style={{ padding: "13px 19px", borderTop: `1px solid ${D.border}`, display: "flex", gap: 9, justifyContent: "flex-end" }}>
            {footer}
          </div>
        )}
      </div>
    </div>
  );
}

// ── KV + Section helpers ──────────────────────────────────────────────────────
function KV({ label, value, mono, D }) {
  return (
    <div style={{ background: D.panel3, border: `1px solid ${D.border}`, borderRadius: 8, padding: "8px 10px" }}>
      <div style={{ fontSize: 9.5, textTransform: "uppercase", letterSpacing: .4, color: D.txtDim, fontWeight: 600 }}>{label}</div>
      <div style={{ fontSize: 12.5, fontWeight: 600, marginTop: 2, color: D.txt, fontFamily: mono ? "inherit" : "inherit", wordBreak: "break-word" }}>
        {value ?? "—"}
      </div>
    </div>
  );
}

function Section({ title, C, D, children }) {
  return (
    <div style={{ marginBottom: 20 }}>
      <div style={{
        fontSize: 10, textTransform: "uppercase", letterSpacing: .7, color: D.txtDim,
        fontWeight: 600, marginBottom: 10, display: "flex", alignItems: "center", gap: 7,
      }}>
        {title}
        <div style={{ flex: 1, height: 1, background: D.border }} />
      </div>
      {children}
    </div>
  );
}

// ── Org Detail Drawer ─────────────────────────────────────────────────────────
function OrgDrawer({ detail, detailLoading, onClose, onSuspend, onRestore, onDelete, onImpersonate, D, isDark }) {
  const org = detail?.organization;
  return (
    <>
      <div onClick={onClose} style={{
        position: "fixed", inset: 0, background: D.overlayBg, backdropFilter: "blur(2px)", zIndex: 60,
      }} />
      <aside style={{
        position: "fixed", top: 0, right: 0, bottom: 0, width: 440, maxWidth: "94vw", zIndex: 70,
        background: `linear-gradient(180deg,${D.panel2},${D.panel})`,
        borderLeft: `1px solid ${D.borderStrong}`, boxShadow: D.shadow,
        display: "flex", flexDirection: "column", fontFamily: "inherit",
      }}>
        {/* Drawer head */}
        <div style={{ padding: "17px 19px", borderBottom: `1px solid ${D.border}`, display: "flex", alignItems: "flex-start", gap: 12 }}>
          <div style={{
            width: 40, height: 40, borderRadius: 10, background: D.navy, display: "grid",
            placeItems: "center", fontWeight: 700, fontSize: 16, color: "#fff", flexShrink: 0,
            border: `1px solid ${D.borderStrong}`,
          }}>{org?.name?.[0] || "?"}</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <h3 style={{ margin: 0, fontSize: 15, fontWeight: 600, color: D.txt, letterSpacing: "-.2px" }}>{org?.name || "Loading…"}</h3>
            <div style={{ fontSize: 11, color: D.txtDim, marginTop: 2, fontFamily: "inherit" }}>
              {org?.id} · {org?.domain || "no domain"}
            </div>
            {org && (
              <div style={{ marginTop: 7, display: "flex", gap: 6, flexWrap: "wrap" }}>
                <StatePillNew state={org.commercial_state} D={D} />
              </div>
            )}
          </div>
          <button onClick={onClose} style={{
            width: 32, height: 32, borderRadius: 8, display: "grid", placeItems: "center",
            border: `1px solid ${D.border}`, background: D.panel3, cursor: "pointer", color: D.txtMute,
          }}>✕</button>
        </div>

        {/* Drawer body */}
        <div style={{ overflowY: "auto", padding: "17px 19px", flex: 1 }}>
          {detailLoading || !org ? (
            <div style={{ padding: 28, textAlign: "center", color: D.txtMute }}>Loading…</div>
          ) : (
            <>
              {/* Metric cards */}
              <Section title="Live Metrics" D={D}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
                  <KV label="Total Users"    value={org.user_count ?? 0}        D={D} />
                  <KV label="Active Users"   value={org.active_users ?? 0}      D={D} />
                  <KV label="Admins"         value={org.admin_count ?? 0}       D={D} />
                  <KV label="Teams"          value={org.team_count ?? 0}        D={D} />
                  <KV label="Pending Leave"  value={detail.pending_leave_count ?? 0} D={D} />
                  <KV label="Clocked In Now" value={detail.active_clock_sessions ?? 0} D={D} />
                </div>
              </Section>

              {/* Account */}
              <Section title="Account" D={D}>
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
                  <KV label="Country"   value={org.country || "—"}                    D={D} />
                  <KV label="Timezone"  value={detail.org_settings?.default_timezone || "—"} D={D} />
                  <KV label="Currency"  value={detail.org_settings?.default_currency || "—"} D={D} />
                  <KV label="Created"   value={org.created_at ? new Date(org.created_at).toLocaleDateString() : "—"} D={D} />
                  <KV label="Last Active" value={org.last_active ? new Date(org.last_active).toLocaleDateString() : "—"} D={D} />
                  <KV label="Org ID"    value={org.id} mono D={D} />
                </div>
              </Section>

              {/* Subscription */}
              {org.subscription && (
                <Section title="Subscription" D={D}>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
                    <KV label="Plan"     value={org.subscription.plan_code || "—"}     D={D} />
                    <KV label="Billing"  value={org.subscription.billing_cycle || "—"} D={D} />
                    <KV label="State"    value={<StatePillNew state={org.subscription.state} D={D} />} D={D} />
                    <KV label="Seats"    value={org.subscription.seat_count ?? "—"}    D={D} />
                    {org.subscription.trial_ends_at && (
                      <KV label="Trial Ends" value={new Date(org.subscription.trial_ends_at).toLocaleDateString()} D={D} />
                    )}
                    {org.subscription.active_since && (
                      <KV label="Active Since" value={new Date(org.subscription.active_since).toLocaleDateString()} D={D} />
                    )}
                  </div>
                </Section>
              )}

              {/* Users table */}
              {(detail.users?.length ?? 0) > 0 && (
                <Section title={`Users (${detail.users.length})`} D={D}>
                  <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                    {detail.users.map(u => (
                      <div key={u.id} style={{
                        display: "flex", alignItems: "center", justifyContent: "space-between",
                        padding: "8px 10px", borderRadius: 8, background: D.panel3,
                        border: `1px solid ${D.border}`,
                      }}>
                        <div>
                          <div style={{ fontSize: 12.5, fontWeight: 600, color: D.txt }}>{u.name}</div>
                          <div style={{ fontSize: 10.5, color: D.txtDim, fontFamily: "inherit" }}>{u.email}</div>
                        </div>
                        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          <Badge cls={u.role === "admin" || u.role === "hr" ? "blue" : "mute"} label={u.role} D={D} />
                          <button onClick={() => onImpersonate(u, org.name)} style={{
                            fontSize: 11, padding: "3px 8px", borderRadius: 6, cursor: "pointer",
                            border: `1px solid ${D.border}`, background: "transparent", color: D.blue, fontWeight: 600,
                          }}>Impersonate</button>
                        </div>
                      </div>
                    ))}
                  </div>
                </Section>
              )}

              {/* Teams */}
              {(detail.teams?.length ?? 0) > 0 && (
                <Section title={`Teams (${detail.teams.length})`} D={D}>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                    {detail.teams.map(t => (
                      <span key={t.id} style={{
                        padding: "4px 12px", borderRadius: 999,
                        background: D.blueSoft, color: D.blue, fontSize: 12.5, fontWeight: 600,
                      }}>{t.name}</span>
                    ))}
                  </div>
                </Section>
              )}

              {/* Recent activity */}
              {(detail.recent_activity?.length ?? 0) > 0 && (
                <Section title="Recent Activity" D={D}>
                  <div style={{ position: "relative", paddingLeft: 16 }}>
                    <div style={{ position: "absolute", left: 4, top: 4, bottom: 4, width: 1, background: D.borderStrong }} />
                    {detail.recent_activity.slice(0, 5).map(a => (
                      <div key={a.id} style={{ position: "relative", marginBottom: 11 }}>
                        <div style={{
                          position: "absolute", left: -14, top: 4, width: 8, height: 8,
                          borderRadius: "50%", background: D.panel, border: `2px solid ${D.blue}`,
                        }} />
                        <div style={{ fontSize: 11.5, fontWeight: 600, color: D.txt }}>{a.action}</div>
                        <div style={{ fontSize: 10, color: D.txtDim, marginTop: 1 }}>
                          {a.entity} · {a.occurred_at ? new Date(a.occurred_at).toLocaleString() : "—"}
                        </div>
                      </div>
                    ))}
                  </div>
                </Section>
              )}

              {/* Debug IDs */}
              <details style={{ marginTop: 8 }}>
                <summary style={{ cursor: "pointer", fontSize: 11, fontWeight: 600, color: D.txtMute, textTransform: "uppercase", letterSpacing: .5, userSelect: "none" }}>
                  ▸ Debug / IDs
                </summary>
                <div style={{ marginTop: 8, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 9 }}>
                  <KV label="Operational ID"  value={String(org.id)} mono D={D} />
                  <KV label="Commercial ID"   value={org.commercial_org_id || "—"} mono D={D} />
                  {org.domain && <KV label="Domain" value={org.domain} mono D={D} />}
                </div>
              </details>
            </>
          )}
        </div>

        {/* Drawer footer — lifecycle actions.
            ACTIVE    → Suspend
            SUSPENDED → Reactivate + Delete
            Delete only ever appears on a suspended organization because the
            backend refuses active → deleted; showing it earlier would offer an
            action that always 409s. */}
        {org && (
          <div style={{ padding: "13px 19px", borderTop: `1px solid ${D.border}`, display: "grid", gap: 8 }}>
            {org.commercial_state === "suspended" ? (
              <>
                <button onClick={() => onRestore(org.id, org.name)} style={{
                  height: 36, borderRadius: 9, background: D.blue, border: "none",
                  color: "#fff", fontWeight: 700, fontSize: 13, cursor: "pointer", fontFamily: "inherit",
                }}>Reactivate Organization</button>
                <button onClick={() => onDelete(org.id, org.name)} style={{
                  height: 36, borderRadius: 9, background: D.bad,
                  border: `1px solid ${D.bad}`, color: "#fff", fontWeight: 700,
                  fontSize: 13, cursor: "pointer", fontFamily: "inherit",
                }}>Delete Organization</button>
              </>
            ) : (
              <button onClick={() => onSuspend(org.id, org.name)} style={{
                height: 36, borderRadius: 9, background: D.badSoft,
                border: `1px solid rgba(240,89,79,.3)`, color: D.bad, fontWeight: 700,
                fontSize: 13, cursor: "pointer", fontFamily: "inherit",
              }}>Suspend Organization</button>
            )}
          </div>
        )}
      </aside>
    </>
  );
}

// ── Isolated 1 Hz ticker — state lives here, NOT on the root component ────────
// Keeping syncSec in its own component means only this tiny element re-renders
// every second. The root SuperAdmin tree (org table, stats, modals) is untouched.
function SyncTicker({ D }) {
  const [syncSec, setSyncSec] = useState(0);
  useEffect(() => {
    const id = setInterval(() => setSyncSec(s => (s + 1) % 60), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 11, color: D.txtMute }}>
      <span style={{ width: 6, height: 6, borderRadius: "50%", background: D.ok }} />
      <span style={{ fontFamily: "inherit" }}>live · {syncSec}s</span>
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────
export default function SuperAdmin() {
  const { isDark, toggleDark } = useTheme();
  const D = useMemo(() => tokens(isDark), [isDark]);
  const navigate = useNavigate();
  const { logout: authLogout } = useAuth();

  const me = useMemo(() => {
    try {
      return {
        name:  localStorage.getItem("name") || "Super Admin",
        email: localStorage.getItem("email") || "",
      };
    } catch { return { name: "Super Admin", email: "" }; }
  }, []);

  const initials = me.name.split(" ").map(w => w[0]).join("").slice(0,2).toUpperCase() || "SA";

  // ── Tab state ────────────────────────────────────────────────────────────
  const [tab, setTab] = useState("organizations");

  // ── Stats ────────────────────────────────────────────────────────────────
  const [stats, setStats]         = useState(null);
  const [statsLoading, setStatsLoading] = useState(true);

  const loadStats = useCallback(async () => {
    setStatsLoading(true);
    try { setStats(await sa("/stats")); }
    catch (e) { toast.error(e.message || "Couldn't load stats"); }
    finally { setStatsLoading(false); }
  }, []);

  // ── Organizations ────────────────────────────────────────────────────────
  const [orgs, setOrgs]           = useState([]);
  const [orgsTotal, setOrgsTotal] = useState(0);
  const [orgsLoading, setOrgsLoading] = useState(false);
  const [orgSearch, setOrgSearch] = useState("");
  const [orgState,  setOrgState]  = useState("");
  const [orgView,   setOrgView]   = useState("all");

  const loadOrgs = useCallback(async () => {
    setOrgsLoading(true);
    try {
      const p = new URLSearchParams();
      if (orgSearch.trim()) p.set("search", orgSearch.trim());
      if (orgState)         p.set("state", orgState);
      p.set("limit", "100");
      const data = await sa(`/organizations?${p.toString()}`);
      setOrgs(data.items || []);
      setOrgsTotal(data.total || 0);
    } catch (e) { toast.error(e.message || "Couldn't load organizations"); }
    finally { setOrgsLoading(false); }
  }, [orgSearch, orgState]);

  useEffect(() => {
    const t = setTimeout(loadOrgs, 250);
    return () => clearTimeout(t);
  }, [loadOrgs]);

  // Pre-compute health scores once when orgs/view changes — not on every render.
  // Each row in the table reads o._health instead of calling healthFromOrg(o) live.
  const filteredOrgs = useMemo(() => {
    const withHealth = orgs.map(o => ({ ...o, _health: healthFromOrg(o) }));
    if (orgView === "all")       return withHealth;
    if (orgView === "active")    return withHealth.filter(o => o.commercial_state === "active");
    if (orgView === "trial")     return withHealth.filter(o => o.commercial_state?.includes("trial"));
    if (orgView === "suspended") return withHealth.filter(o => o.commercial_state === "suspended");
    if (orgView === "atrisk")    return withHealth.filter(o => healthBand(o._health) === "atrisk");
    if (orgView === "pastdue")   return withHealth.filter(o => o.commercial_state === "past_due");
    return withHealth;
  }, [orgs, orgView]);

  // ── Signups ──────────────────────────────────────────────────────────────
  const [signups, setSignups]         = useState([]);
  const [signupsLoading, setSignupsLoading] = useState(false);

  const loadSignups = useCallback(async () => {
    setSignupsLoading(true);
    try { const data = await sa("/signups?limit=100"); setSignups(data.items || []); }
    catch (e) { toast.error(e.message || "Couldn't load signups"); }
    finally { setSignupsLoading(false); }
  }, []);

  // ── Audit ────────────────────────────────────────────────────────────────
  const [audit, setAudit]           = useState([]);
  const [auditLoading, setAuditLoading] = useState(false);
  const [auditWindow, setAuditWindow] = useState(168);
  const [auditSearch, setAuditSearch] = useState("");
  const [auditActions, setAuditActions] = useState("");

  const loadAudit = useCallback(async () => {
    setAuditLoading(true);
    try {
      const p = new URLSearchParams();
      p.set("since_hours", String(auditWindow));
      if (auditActions.trim()) p.set("actions", auditActions.trim());
      const data = await sa(`/audit?${p.toString()}`);
      setAudit(data.events || []);
    } catch (e) { toast.error(e.message || "Couldn't load audit trail"); }
    finally { setAuditLoading(false); }
  }, [auditWindow, auditActions]);

  const filteredAudit = useMemo(() => {
    if (!auditSearch.trim()) return audit;
    const q = auditSearch.toLowerCase();
    return audit.filter(e =>
      (e.actor_email + e.entity + e.action + JSON.stringify(e.payload || {})).toLowerCase().includes(q)
    );
  }, [audit, auditSearch]);

  // ── Billing ──────────────────────────────────────────────────────────────
  const [billing, setBilling]             = useState(null);
  const [billingLoading, setBillingLoading] = useState(false);

  const loadBilling = useCallback(async () => {
    setBillingLoading(true);
    try { const data = await sa("/billing"); setBilling(data); }
    catch (e) { toast.error(e.message || "Couldn't load billing data"); }
    finally { setBillingLoading(false); }
  }, []);

  // ── Create org form ──────────────────────────────────────────────────────
  const FORM_EMPTY = {
    name: "", slug: "", company_name: "",
    admin_email: "", admin_name: "",
    timezone: "", currency: "", country: "",
    seed_default_teams: false, default_teams_raw: "",
    seed_leave_policies: true, shared_pool_enabled: true,
    leave_waiting_period_months: 6,
  };
  const [form, setForm]           = useState(FORM_EMPTY);
  const [slugManual, setSlugManual] = useState(false);
  const [createLoading, setCreateLoading] = useState(false);
  const [createResult, setCreateResult]   = useState(null);
  const [createError,  setCreateError]    = useState("");
  const [fieldErrors,  setFieldErrors]    = useState({});

  const SLUG_RE = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

  const setField = (k, v) => {
    setForm(prev => {
      const next = { ...prev, [k]: v };
      if (k === "name" && !slugManual) next.slug = slugify(v);
      return next;
    });
    if (fieldErrors[k]) setFieldErrors(prev => { const n = { ...prev }; delete n[k]; return n; });
  };

  const handleCreateOrg = async (e) => {
    e.preventDefault();
    setCreateError(""); setCreateResult(null);

    const orgName    = form.name.trim();
    const adminEmail = form.admin_email.trim().toLowerCase();
    const slug       = form.slug.trim();

    // Field-level validation — mirrors backend pre-flight checks
    const errs = {};
    if (!orgName) errs.name = "Organization name is required.";
    if (!adminEmail) {
      errs.admin_email = "Administrator email address is required.";
    } else if (!adminEmail.includes("@") || !adminEmail.includes(".")) {
      errs.admin_email = "Please enter a valid email address.";
    }
    if (slug && !SLUG_RE.test(slug)) {
      errs.slug = "Use lowercase letters, numbers, and hyphens only (e.g. acme-corp).";
    }

    if (Object.keys(errs).length) {
      setFieldErrors(errs);
      setCreateError("Please fix the highlighted fields before continuing.");
      return;
    }

    setFieldErrors({});
    const teams = form.seed_default_teams
      ? form.default_teams_raw.split(",").map(t => t.trim()).filter(Boolean)
      : undefined;

    const payload = {
      name:                        orgName,
      slug:                        slug || undefined,
      company_name:                form.company_name.trim() || undefined,
      admin_email:                 adminEmail,
      admin_name:                  form.admin_name.trim() || undefined,
      timezone:                    form.timezone || undefined,
      currency:                    form.currency || undefined,
      country:                     form.country || undefined,
      seed_leave_policies:         form.seed_leave_policies,
      seed_default_teams:          form.seed_default_teams,
      default_teams:               teams,
      shared_pool_enabled:         form.country === "IN" ? form.shared_pool_enabled : false,
      leave_waiting_period_months: Number(form.leave_waiting_period_months) || 6,
    };

    setCreateLoading(true);
    try {
      const result = await sa("/organizations", { method: "POST", body: JSON.stringify(payload) });
      setCreateResult(result);
      setForm(FORM_EMPTY); setSlugManual(false); setFieldErrors({});
      loadStats(); loadOrgs();
      toast.success(`"${orgName}" has been set up successfully.`);
    } catch (err) {
      const msg = err.message || "";
      if (msg.toLowerCase().includes("email") && msg.toLowerCase().includes("active")) {
        setFieldErrors({ admin_email: "This email address already belongs to an active account." });
        setCreateError("Please use a different administrator email address.");
      } else if (msg.toLowerCase().includes("slug")) {
        setFieldErrors({ slug: "This workspace identifier is already in use. Please choose another." });
        setCreateError("Please fix the highlighted fields before continuing.");
      } else {
        setCreateError(msg || "Organization setup failed. Please try again or contact support.");
      }
    } finally { setCreateLoading(false); }
  };

  // ── Org detail drawer ────────────────────────────────────────────────────
  const [detail, setDetail]           = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);

  const openOrgDetail = useCallback(async (orgId) => {
    setDetail({ organization: { id: orgId } });
    setDetailLoading(true);
    try { setDetail(await sa(`/organizations/${encodeURIComponent(orgId)}`)); }
    catch (e) { toast.error(e.message || "Couldn't load organization"); setDetail(null); }
    finally { setDetailLoading(false); }
  }, []);

  // ── Suspend / restore ────────────────────────────────────────────────────
  const [stateModal, setStateModal]   = useState(null);
  const [stateReason, setStateReason] = useState("");
  const [stateWorking, setStateWorking] = useState(false);

  const performStateChange = async () => {
    if (!stateModal) return;
    setStateWorking(true);
    try {
      await sa(`/organizations/${encodeURIComponent(stateModal.orgId)}/${stateModal.action}`, {
        method: "POST", body: JSON.stringify({ reason: stateReason }),
      });
      toast.success(stateModal.action === "suspend" ? "Organization suspended" : "Organization restored");
      setStateModal(null); setStateReason("");
      loadOrgs(); loadStats();
      if (detail?.organization?.id === stateModal.orgId) openOrgDetail(stateModal.orgId);
    } catch (e) { toast.error(e.message || "Operation failed"); }
    finally { setStateWorking(false); }
  };

  // ── Delete organization ──────────────────────────────────────────────────
  // Destructive and irreversible, so the guard rails are deliberate:
  //   • only reachable from a SUSPENDED organization (the backend enforces this
  //     too — the UI is not the security boundary)
  //   • the exact organization name must be typed to arm the button
  //   • the typed name is sent to the server, which re-checks it
  const [deleteModal, setDeleteModal]   = useState(null);
  const [deleteTyped, setDeleteTyped]   = useState("");
  const [deleteWorking, setDeleteWorking] = useState(false);

  const deleteArmed =
    !!deleteModal && deleteTyped.trim() === (deleteModal.name || "").trim();

  const performDelete = async () => {
    if (!deleteModal || !deleteArmed) return;
    setDeleteWorking(true);
    try {
      const result = await sa(`/organizations/${encodeURIComponent(deleteModal.orgId)}`, {
        method: "DELETE",
        body: JSON.stringify({ confirm_name: deleteTyped.trim() }),
      });
      // Drop it from the table immediately rather than waiting for the refetch,
      // so the row cannot linger on a stale list after a successful delete.
      setOrgs(prev => prev.filter(o => o.id !== deleteModal.orgId));
      setOrgsTotal(t => Math.max(0, t - 1));
      if (detail?.organization?.id === deleteModal.orgId) setDetail(null);
      toast.success(
        `"${deleteModal.name}" deleted — ${(result?.rows_deleted ?? 0).toLocaleString()} records removed.`
      );
      setDeleteModal(null); setDeleteTyped("");
      loadOrgs(); loadStats();
    } catch (e) {
      // Failure leaves the organization visible and intact — the backend rolls
      // the whole purge back, so the list must not be optimistically pruned.
      toast.error(e.message || "Deletion failed — the organization is unchanged.");
    } finally { setDeleteWorking(false); }
  };

  // ── Impersonation ────────────────────────────────────────────────────────
  const [impModal, setImpModal]     = useState(null);
  const [impReason, setImpReason]   = useState("");
  const [impWorking, setImpWorking] = useState(false);

  const performImpersonate = async () => {
    if (!impModal) return;
    setImpWorking(true);
    try {
      const result = await sa("/impersonate", {
        method: "POST",
        body: JSON.stringify({ user_id: impModal.user.id, reason: impReason || "Super-admin support investigation", duration_minutes: 30 }),
      });
      try {
        localStorage.setItem("zt_sa_return_token", TOKEN());
        localStorage.setItem("zt_sa_return_role", "super_admin");
      } catch {}
      writeToken(result.access_token, { remember: true });
      const u = result.target_user;
      mirrorUserToStorage({
        id: u.id, name: u.name || "", email: u.email || "",
        role: u.role || "employee", profile_picture: u.profile_picture || null,
        organization_id: u.organization_id ?? null,
      });
      toast.success(`Signed in as ${u.email}. Use the red banner to return.`);
      setImpModal(null); setImpReason("");
      window.location.assign(u.role === "admin" || u.role === "hr" ? "/admin" : "/dashboard");
    } catch (e) { toast.error(e.message || "Impersonation failed"); }
    finally { setImpWorking(false); }
  };

  const signOut = () => {
    // Use the shared AuthContext logout so POST /auth/logout is called first,
    // which invalidates the refresh token cookie on the server. Skipping this
    // step caused the super_admin to be silently re-authenticated via the cookie
    // and redirected back to /super-admin immediately after sign-out.
    authLogout({ redirect: true });
  };

  // ── Initial loads ────────────────────────────────────────────────────────
  useEffect(() => { loadStats(); }, [loadStats]);
  useEffect(() => {
    if (tab === "signups" && signups.length === 0) loadSignups();
    if (tab === "audit"   && audit.length === 0)   loadAudit();
    if (tab === "billing" && !billing)             loadBilling();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tab]);

  // ── Computed stats ───────────────────────────────────────────────────────
  const totalOrgs        = stats?.organizations?.total      ?? 0;
  const activeOrgs       = stats?.organizations?.active     ?? 0;
  const trialOrgs        = stats?.organizations?.trial      ?? 0;
  const suspOrgs         = stats?.organizations?.suspended  ?? 0;
  const pastDueOrgs      = stats?.organizations?.past_due   ?? 0;
  const totalUsers       = stats?.users?.total              ?? 0;
  const activeUsers      = stats?.users?.active_today       ?? 0;
  const signups7d        = stats?.signups?.last_7d          ?? 0;
  const billableSeats    = stats?.platform?.billable_seats         ?? 0;
  const privilegedAdmins = stats?.platform?.privileged_admin_users ?? 0;
  const activeCountries  = stats?.platform?.active_countries       ?? 0;
  const openSecEvents    = stats?.platform?.open_security_events   ?? 0;

  const tabsDef = [
    { k: "organizations", label: "Organizations",         count: orgsTotal || totalOrgs },
    { k: "signups",       label: "Trial & Signup Pipeline", count: signups.length || signups7d },
    { k: "audit",         label: "Audit Trail",           count: audit.length },
    { k: "billing",       label: "Billing & Subscriptions", count: null },
    { k: "create_org",    label: "Create New Organization", count: null },
  ];

  // ── Shared input style — memoized on theme change only ──────────────────
  const { inp, sel, tblTh, tblTd } = useMemo(() => {
    const _inp = {
      background: D.panel3, border: `1px solid ${D.border}`, borderRadius: 9,
      padding: "9px 11px", fontSize: 12.5, outline: "none", color: D.txt,
      fontFamily: "inherit", width: "100%", boxSizing: "border-box",
    };
    return {
      inp: _inp,
      sel: {
        ..._inp, height: 34, padding: "0 30px 0 11px", cursor: "pointer", appearance: "none",
        backgroundImage: `url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 24 24' fill='none' stroke='%238392B0' stroke-width='3'><path d='M6 9l6 6 6-6'/></svg>")`,
        backgroundRepeat: "no-repeat", backgroundPosition: "right 10px center",
        backgroundSize: "10px",
      },
      tblTh: {
        textAlign: "left", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .6,
        color: D.txtDim, fontWeight: 600, padding: "12px 16px",
        borderBottom: `1px solid ${D.border}`, whiteSpace: "nowrap",
        background: D.panel3, position: "sticky", top: 0, zIndex: 1,
      },
      tblTd: { padding: "15px 16px", fontSize: 12.5, whiteSpace: "nowrap", verticalAlign: "middle", color: D.txt },
    };
  }, [D]);

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div style={{
      minHeight: "100vh", color: D.txt,
      background: D.bgGrad,
      fontFamily: "inherit",
      fontSize: 13, lineHeight: 1.45, WebkitFontSmoothing: "antialiased",
    }}>

      {/* ── Topbar ──────────────────────────────────────────────────── */}
      <header style={{
        display: "flex", alignItems: "center", gap: 18, padding: "0 22px", height: 58,
        background: `linear-gradient(180deg,${D.panel2},${D.panel})`,
        borderBottom: `1px solid ${D.border}`,
        position: "sticky", top: 0, zIndex: 40,
      }}>
        {/* Brand */}
        <img src={isDark ? TemverisLogoWhite : TemverisLogo} alt="Temveris" style={{ height: 32, width: "auto" }} />

        {/* Page title */}
        <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.2, marginLeft: 4 }}>
          <b style={{ fontSize: 14, fontWeight: 600, letterSpacing: "-.2px", color: D.txt }}>Platform Operations Command Center</b>
          <span style={{ fontSize: 11, color: D.txtDim }}>Organization health · subscriptions · onboarding · security posture</span>
        </div>

        <div style={{ flex: 1 }} />


        {/* Sync ticker — isolated child so its 1 Hz state never re-renders the root */}
        <SyncTicker D={D} />

        {/* Theme toggle */}
        <button onClick={toggleDark} title="Toggle theme" style={{
          width: 32, height: 32, borderRadius: 8, display: "grid", placeItems: "center",
          border: `1px solid ${D.border}`, background: D.panel3, cursor: "pointer", color: D.txtMute,
          transition: ".15s",
        }}>{isDark ? "☀" : "☾"}</button>

        {/* Profile */}
        <div style={{ display: "flex", alignItems: "center", gap: 9, paddingLeft: 14, borderLeft: `1px solid ${D.border}` }}>
          <div style={{
            width: 30, height: 30, borderRadius: 8, background: D.navy, display: "grid",
            placeItems: "center", fontWeight: 600, fontSize: 12, color: "#fff",
            border: `1px solid ${D.borderStrong}`,
          }}>{initials}</div>
          <div style={{ lineHeight: 1.25 }}>
            <b style={{ fontSize: 11.5, fontWeight: 600, display: "block", color: D.txt }}>{me.name}</b>
            <span style={{ fontSize: 10, color: D.txtDim }}>{me.email}</span>
          </div>
          <button onClick={signOut} title="Sign out" style={{
            width: 32, height: 32, borderRadius: 8, display: "grid", placeItems: "center",
            border: `1px solid ${D.border}`, background: D.panel3, cursor: "pointer", color: D.txtMute,
          }}>
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/>
            </svg>
          </button>
        </div>
      </header>

      {/* animations: pulse + staggered card entrance (opacity only, so hover transform stays free) */}
      <style>{`
        @keyframes saPulse{0%{box-shadow:0 0 0 0 rgba(240,89,79,.55)}70%{box-shadow:0 0 0 7px rgba(240,89,79,0)}100%{box-shadow:0 0 0 0 rgba(240,89,79,0)}}
        @keyframes saFade{from{opacity:0}to{opacity:1}}
        .sa-kpi-grid>*,.sa-mini-grid>*{animation:saFade .5s both}
        .sa-kpi-grid>*:nth-child(1){animation-delay:.02s}
        .sa-kpi-grid>*:nth-child(2){animation-delay:.07s}
        .sa-kpi-grid>*:nth-child(3){animation-delay:.12s}
        .sa-kpi-grid>*:nth-child(4){animation-delay:.17s}
        .sa-kpi-grid>*:nth-child(5){animation-delay:.22s}
        .sa-mini-grid>*:nth-child(1){animation-delay:.26s}
        .sa-mini-grid>*:nth-child(2){animation-delay:.30s}
        .sa-mini-grid>*:nth-child(3){animation-delay:.34s}
        .sa-mini-grid>*:nth-child(4){animation-delay:.38s}
        .sa-mini-grid>*:nth-child(5){animation-delay:.42s}
        .sa-mini-grid>*:nth-child(6){animation-delay:.46s}
        @media (prefers-reduced-motion: reduce){.sa-kpi-grid>*,.sa-mini-grid>*{animation:none}}
      `}</style>

      {/* ── Shell ───────────────────────────────────────────────────── */}
      <div style={{ padding: "18px 22px 60px", maxWidth: 1640, margin: "0 auto" }}>

        {/* ── KPI strip ─────────────────────────────────────────────── */}
        <div className="sa-kpi-grid" style={{ display: "grid", gridTemplateColumns: "repeat(5,1fr)", gap: 12 }}>
          <KpiCard D={D} label="Total Tenants" value={totalOrgs} loading={statsLoading} icon={Icon.building(16)}
            accent={D.blue} delta="+0" dir="flat" cta="View all organizations"
            onClick={() => { setTab("organizations"); setOrgView("all"); }} />
          <KpiCard D={D} label="Active Tenants" value={activeOrgs} loading={statsLoading} icon={Icon.activity(16)}
            accent={D.ok} delta="+0" dir="flat" cta="View active organizations"
            onClick={() => { setTab("organizations"); setOrgView("active"); }} />
          <KpiCard D={D} label="Revenue Risk" value={pastDueOrgs} loading={statsLoading} icon={Icon.alert(16)}
            accent={D.bad} delta="+0" dir="flat" cta="Past due & at risk"
            onClick={() => { setTab("organizations"); setOrgView("pastdue"); }} />
          <KpiCard D={D} label="Trial Pipeline" value={trialOrgs} loading={statsLoading} icon={Icon.rocket(16)}
            accent={D.teal} delta="+0" dir="flat" cta="View trial pipeline"
            onClick={() => { setTab("organizations"); setOrgView("trial"); }} />
          <KpiCard D={D} label="Restricted / Suspended" value={suspOrgs} loading={statsLoading} icon={Icon.ban(16)}
            accent={D.warn} delta="+0" dir="flat" cta="View suspended organizations"
            onClick={() => { setTab("organizations"); setOrgView("suspended"); }} />
        </div>

        {/* ── Secondary Platform Health Row ─────────────────────────── */}
        <div style={{ marginTop: 14 }}>
          <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: .7, color: D.txtDim, fontWeight: 600, marginBottom: 8 }}>Platform Health</div>
          <div className="sa-mini-grid" style={{ display: "grid", gridTemplateColumns: "repeat(6,1fr)", gap: 12 }}>
            <MiniKpi D={D} accent={D.blue} icon={Icon.users(14)}    loading={statsLoading} label="Users"        value={totalUsers}       sub="users" />
            <MiniKpi D={D} accent={D.ok}   icon={Icon.activity(14)} loading={statsLoading} label="Sessions"     value={activeUsers}      sub="sessions" />
            <MiniKpi D={D} accent={D.teal} icon={Icon.layers(14)}   loading={statsLoading} label="Seats"        value={billableSeats}    sub="seats" />
            <MiniKpi D={D} accent={D.warn} icon={Icon.shield(14)}   loading={statsLoading} label="Admin users"  value={privilegedAdmins} sub="admin users" />
            <MiniKpi D={D} accent={D.blue} icon={Icon.globe(14)}    loading={statsLoading} label="Countries"    value={activeCountries}  sub="countries" />
            <MiniKpi D={D} accent={D.bad}  icon={Icon.zap(14)}      loading={statsLoading} label="Events"       value={openSecEvents}    sub="events" />
          </div>
        </div>

        {/* ── Tabs ──────────────────────────────────────────────────── */}
        <div style={{ display: "flex", gap: 3, margin: "18px 0 0", borderBottom: `1px solid ${D.border}`, flexWrap: "wrap" }}>
          {tabsDef.map(t => <TabBtn key={t.k} t={t} active={tab === t.k} onClick={() => setTab(t.k)} D={D} />)}
        </div>

        {/* ── Tab content ───────────────────────────────────────────── */}
        <div style={{ marginTop: 16 }}>

          {/* Organizations tab */}
          {tab === "organizations" && (
            <div style={{ background: D.panel, border: `1px solid ${D.border}`, borderRadius: 13, overflow: "hidden" }}>
              {/* Toolbar */}
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "13px 15px", borderBottom: `1px solid ${D.border}` }}>
                <div style={{
                  display: "flex", alignItems: "center", gap: 8, background: D.panel3,
                  border: `1px solid ${D.border}`, borderRadius: 9, padding: "0 11px",
                  height: 34, flex: 1, maxWidth: 420,
                }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={D.txtDim} strokeWidth="2">
                    <circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>
                  </svg>
                  <input value={orgSearch} onChange={e => setOrgSearch(e.target.value)}
                    placeholder="Search by name, legal name, or domain…"
                    style={{ background: "none", border: "none", outline: "none", fontSize: 12.5, width: "100%", color: D.txt, fontFamily: "inherit" }} />
                </div>
                <select value={orgState} onChange={e => setOrgState(e.target.value)} style={{ ...sel, width: "auto", minWidth: 130 }}>
                  <option value="">All states</option>
                  <option value="active">Active</option>
                  <option value="trial_active">Trial Active</option>
                  <option value="trial_expired">Trial Expired</option>
                  <option value="past_due">Past Due</option>
                  <option value="suspended">Suspended</option>
                  <option value="cancelled">Cancelled</option>
                </select>
                <button onClick={() => { setOrgSearch(""); setOrgState(""); setOrgView("all"); }} style={{
                  height: 34, padding: "0 13px", borderRadius: 9, fontSize: 12, fontWeight: 600,
                  background: "none", border: `1px solid ${D.border}`, color: D.txt, cursor: "pointer", fontFamily: "inherit",
                }}>Clear</button>
                <button onClick={loadOrgs} style={{
                  height: 34, padding: "0 13px", borderRadius: 9, fontSize: 12, fontWeight: 600,
                  background: D.panel3, border: `1px solid ${D.border}`, color: D.txt, cursor: "pointer", fontFamily: "inherit",
                  display: "flex", alignItems: "center", gap: 6,
                }}>
                  <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 .49-4.5"/></svg>
                  Refresh
                </button>
              </div>

              {/* Saved views */}
              <div style={{ display: "flex", gap: 6, flexWrap: "wrap", padding: "11px 15px", borderBottom: `1px solid ${D.border}`, alignItems: "center" }}>
                <span style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: .6, color: D.txtDim, fontWeight: 600, marginRight: 4 }}>Saved views</span>
                {[
                  ["all",       "All Organizations", orgsTotal, D.blue],
                  ["active",    "Active",            activeOrgs, D.ok],
                  ["trial",     "Trial",             trialOrgs,  D.teal],
                  ["suspended", "Suspended",         suspOrgs,   D.warn],
                  ["atrisk",    "At Risk",           null,       D.bad],
                  ["pastdue",   "Past Due",          null,       D.bad],
                ].map(([k, lbl, cnt, ac]) => (
                  <SavedViewBtn key={k} label={lbl} count={cnt} active={orgView === k}
                    accent={ac} onClick={() => setOrgView(k)} D={D} />
                ))}
              </div>

              {/* Table */}
              <div style={{ overflowX: "auto" }}>
                {orgsLoading ? (
                  <div style={{ padding: 40, textAlign: "center", color: D.txtMute }}>Loading organizations…</div>
                ) : filteredOrgs.length === 0 ? (
                  <div style={{ padding: 54, textAlign: "center", color: D.txtMute }}>
                    <div style={{ fontSize: 32, marginBottom: 8, opacity: .5 }}>⊘</div>
                    <b style={{ display: "block", color: D.txt, fontSize: 13, marginBottom: 3 }}>No organizations match your filters</b>
                    <span>Adjust the search, filters, or saved view.</span>
                  </div>
                ) : (
                  <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 1200 }}>
                    <thead>
                      <tr>
                        <th style={tblTh}>Organization</th>
                        <th style={tblTh}>Lifecycle</th>
                        <th style={tblTh}>Billing State</th>
                        <th style={tblTh}>Health</th>
                        <th style={tblTh}>Users · Seats</th>
                        <th style={tblTh}>Country</th>
                        <th style={tblTh}>Last Active</th>
                        <th style={tblTh}></th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredOrgs.map((o, i) => {
                        const hs = o._health;  // pre-computed in filteredOrgs useMemo
                        const lastDays = o.last_active
                          ? Math.floor((Date.now() - new Date(o.last_active).getTime()) / 86400000)
                          : null;
                        const zebra = i % 2 === 1 ? D.rowHover : "transparent";  // soft alternating rows
                        return (
                          <tr key={o.id}
                            onClick={() => openOrgDetail(o.id)}
                            style={{ background: zebra, borderBottom: `1px solid ${D.border}`, cursor: "pointer", transition: "background .12s, box-shadow .12s" }}
                            onMouseEnter={e => { e.currentTarget.style.background = D.blueSoft; e.currentTarget.style.boxShadow = `inset 3px 0 0 ${D.blue}`; }}
                            onMouseLeave={e => { e.currentTarget.style.background = zebra; e.currentTarget.style.boxShadow = "none"; }}
                          >
                            <td style={tblTd}>
                              <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
                                <b style={{ fontWeight: 600, fontSize: 12.5 }}>{o.name}</b>
                                <span style={{ fontSize: 10.5, color: D.txtDim, fontFamily: "inherit" }}>
                                  {o.id}{o.domain ? ` · ${o.domain}` : ""}
                                </span>
                              </div>
                            </td>
                            <td style={tblTd}><StatePillNew state={o.commercial_state} D={D} /></td>
                            <td style={tblTd}>
                              {o.subscription
                                ? <StatePillNew state={o.subscription.state} D={D} />
                                : <span style={{ fontSize: 11, color: D.txtDim }}>—</span>}
                            </td>
                            <td style={tblTd}><HealthRing score={hs} D={D} /></td>
                            <td style={tblTd}>
                              <div style={{ display: "flex", flexDirection: "column", gap: 3, minWidth: 80 }}>
                                <span style={{ fontSize: 12, fontFamily: "inherit" }}>
                                  {o.user_count ?? 0}
                                  {o.active_users != null ? ` · ${o.active_users} active` : ""}
                                </span>
                              </div>
                            </td>
                            <td style={{ ...tblTd, color: D.txtMute }}>{o.country || "—"}</td>
                            <td style={{ ...tblTd, fontFamily: "inherit", fontSize: 11, color: lastDays != null && lastDays >= 7 ? D.bad : D.txt }}>
                              {lastDays === null ? "—" : lastDays === 0 ? "Today" : lastDays === 1 ? "Yesterday" : `${lastDays}d ago`}
                            </td>
                            <td style={{ ...tblTd, textAlign: "right" }}>
                              <button onClick={e => { e.stopPropagation(); openOrgDetail(o.id); }} style={{
                                width: 28, height: 28, borderRadius: 7, display: "grid", placeItems: "center",
                                border: `1px solid ${D.border}`, background: D.panel3, cursor: "pointer", color: D.txtMute,
                              }}>
                                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4"><path d="M9 18l6-6-6-6"/></svg>
                              </button>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Table footer */}
              <div style={{
                display: "flex", alignItems: "center", justifyContent: "space-between",
                padding: "11px 15px", borderTop: `1px solid ${D.border}`,
                fontSize: 11.5, color: D.txtMute,
              }}>
                <span>Showing <b style={{ fontFamily: "inherit" }}>{filteredOrgs.length}</b> of <b style={{ fontFamily: "inherit" }}>{orgsTotal}</b> organizations</span>
                <span>Metrics are live aggregates.</span>
              </div>
            </div>
          )}

          {/* Signups tab */}
          {tab === "signups" && (
            <div style={{ background: D.panel, border: `1px solid ${D.border}`, borderRadius: 13, overflow: "hidden" }}>
              <div style={{ padding: "13px 15px", borderBottom: `1px solid ${D.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" }}>
                <div>
                  <div style={{ fontSize: 13, fontWeight: 600, color: D.txt }}>Trial &amp; Signup Pipeline</div>
                  <div style={{ fontSize: 11, color: D.txtMute, marginTop: 2 }}>Signup quality and conversion risk — not just acquisition counts.</div>
                </div>
                <button onClick={loadSignups} style={{
                  height: 34, padding: "0 13px", borderRadius: 9, fontSize: 12, fontWeight: 600,
                  background: D.panel3, border: `1px solid ${D.border}`, color: D.txt, cursor: "pointer", fontFamily: "inherit",
                }}>Refresh</button>
              </div>
              {signupsLoading ? (
                <div style={{ padding: 40, textAlign: "center", color: D.txtMute }}>Loading…</div>
              ) : signups.length === 0 ? (
                <div style={{ padding: 40, textAlign: "center", color: D.txtMute }}>No signup attempts recorded.</div>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 980 }}>
                    <thead>
                      <tr>
                        {["Signed Up","Email","State","Intent","Plan","Country","Organization","IP"].map(h => (
                          <th key={h} style={tblTh}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {signups.map(s => (
                        <tr key={s.id} style={{ borderBottom: `1px solid ${D.border}` }}>
                          <td style={{ ...tblTd, fontFamily: "inherit", fontSize: 11, color: D.txtMute }}>
                            {s.created_at ? new Date(s.created_at).toLocaleString() : "—"}
                          </td>
                          <td style={tblTd}>{s.email}</td>
                          <td style={tblTd}><StatePillNew state={s.state} D={D} /></td>
                          <td style={{ ...tblTd, color: D.txtMute }}>{s.intent || "—"}</td>
                          <td style={{ ...tblTd, color: D.txtMute }}>{s.plan || "—"}</td>
                          <td style={{ ...tblTd, color: D.txtMute }}>{s.country || "—"}</td>
                          <td style={tblTd}>
                            {s.organization_id ? (
                              <button onClick={() => { setTab("organizations"); openOrgDetail(s.organization_id); }} style={{
                                background: "transparent", border: "none", color: D.blue,
                                padding: 0, cursor: "pointer", fontFamily: "inherit", fontWeight: 700,
                              }}>{s.organization_id}</button>
                            ) : "—"}
                          </td>
                          <td style={{ ...tblTd, color: D.txtMute, fontFamily: "inherit", fontSize: 11 }}>{s.ip_address || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div style={{ padding: "11px 15px", borderTop: `1px solid ${D.border}`, fontSize: 11.5, color: D.txtMute, display: "flex", justifyContent: "space-between" }}>
                <span>Showing <b style={{ fontFamily: "inherit" }}>{signups.length}</b> signups</span>
                <span>Last 7 days</span>
              </div>
            </div>
          )}

          {/* Audit tab */}
          {tab === "audit" && (
            <div style={{ background: D.panel, border: `1px solid ${D.border}`, borderRadius: 13, overflow: "hidden" }}>
              <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 10, padding: "13px 15px", borderBottom: `1px solid ${D.border}` }}>
                <div style={{
                  display: "flex", alignItems: "center", gap: 8, background: D.panel3,
                  border: `1px solid ${D.border}`, borderRadius: 9, padding: "0 11px",
                  height: 34, minWidth: 300, flex: 1, maxWidth: 380,
                }}>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke={D.txtDim} strokeWidth="2"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>
                  <input value={auditSearch} onChange={e => setAuditSearch(e.target.value)}
                    placeholder="Search actor, entity, or action…"
                    style={{ background: "none", border: "none", outline: "none", fontSize: 12.5, width: "100%", color: D.txt, fontFamily: "inherit" }} />
                </div>
                <input value={auditActions} onChange={e => setAuditActions(e.target.value)}
                  onBlur={loadAudit} onKeyDown={e => e.key === "Enter" && loadAudit()}
                  placeholder="Filter by action type…"
                  style={{ ...inp, width: "auto", minWidth: 200, height: 34, padding: "0 11px" }} />
                <select value={auditWindow} onChange={e => { setAuditWindow(Number(e.target.value)); }}
                  style={{ ...sel, width: "auto", minWidth: 150 }}>
                  <option value={24}>Last 24 hours</option>
                  <option value={168}>Last 7 days</option>
                  <option value={720}>Last 30 days</option>
                  <option value={2160}>Last 90 days</option>
                </select>
                <button onClick={loadAudit} style={{
                  height: 34, padding: "0 13px", borderRadius: 9, fontSize: 12, fontWeight: 600,
                  background: D.blue, border: `1px solid ${D.blue}`, color: "#fff", cursor: "pointer", fontFamily: "inherit",
                }}>Refresh</button>
              </div>
              {auditLoading ? (
                <div style={{ padding: 40, textAlign: "center", color: D.txtMute }}>Loading…</div>
              ) : filteredAudit.length === 0 ? (
                <div style={{ padding: 40, textAlign: "center", color: D.txtMute }}>No audit events match this filter.</div>
              ) : (
                <div style={{ overflowX: "auto" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 980 }}>
                    <thead>
                      <tr>
                        {["Timestamp","Actor","Entity","Action","Details","IP"].map(h => (
                          <th key={h} style={tblTh}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {filteredAudit.map(e => (
                        <tr key={e.id} style={{ borderBottom: `1px solid ${D.border}` }}>
                          <td style={{ ...tblTd, fontFamily: "inherit", fontSize: 11, color: D.txtMute }}>
                            {e.occurred_at ? new Date(e.occurred_at).toLocaleString() : "—"}
                          </td>
                          <td style={tblTd}>
                            <div style={{ fontWeight: 600, color: D.txt }}>{e.actor_email || "system"}</div>
                            {e.actor_role && <div style={{ fontSize: 10.5, color: D.txtDim }}>{e.actor_role}</div>}
                          </td>
                          <td style={{ ...tblTd, fontWeight: 600 }}>{e.entity}</td>
                          <td style={tblTd}>
                            <b style={{ color: D.blue, fontWeight: 700 }}>{e.action}</b>
                          </td>
                          <td style={{ ...tblTd, color: D.txtMute, maxWidth: 320, overflow: "hidden", textOverflow: "ellipsis" }}>
                            {e.payload ? JSON.stringify(e.payload) : "—"}
                          </td>
                          <td style={{ ...tblTd, fontFamily: "inherit", fontSize: 11, color: D.txtMute }}>{e.ip_address || "—"}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              <div style={{ padding: "11px 15px", borderTop: `1px solid ${D.border}`, fontSize: 11.5, color: D.txtMute, display: "flex", justifyContent: "space-between" }}>
                <span>Immutable operational history — entries cannot be edited or deleted.</span>
                <span style={{ fontFamily: "inherit" }}>{filteredAudit.length} / {audit.length} entries</span>
              </div>
            </div>
          )}

          {/* Billing & Subscriptions tab */}
          {tab === "billing" && (() => {
            const fmtMrr = v => v == null ? "—" : v === 0 ? "$0" : `$${v.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 0 })}`;
            const billingRows = billing?.orgs || [];
            const statusColor = s => {
              if (!s) return { bg: D.panel3, color: D.txtMute };
              if (s === "active") return { bg: D.okSoft, color: D.ok };
              if (s === "trial" || s === "trialing") return { bg: D.blueSoft, color: D.blue };
              if (s === "past_due") return { bg: D.warnSoft, color: D.warn };
              if (s === "failed" || s === "cancel_pending") return { bg: D.badSoft, color: D.bad };
              return { bg: D.panel3, color: D.txtMute };
            };
            const kpis = [
              { label: "PLATFORM MRR",      value: billingLoading ? "…" : fmtMrr(billing?.platform_mrr), sub: "monthly recurring revenue" },
              { label: "ANNUALISED (ARR)",   value: billingLoading ? "…" : fmtMrr(billing?.platform_arr), sub: "annualised run rate" },
              { label: "PAST DUE / FAILED",  value: billingLoading ? "…" : billing ? String(billing.past_due) : "—", sub: "requires action", alert: billing?.past_due > 0 },
              { label: "RENEWALS — 30 DAYS", value: billingLoading ? "…" : billing ? String(billing.renewals_30d) : "—", sub: "renewing soon" },
            ];
            return (
              <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
                <div>
                  <div style={{ fontSize: 16, fontWeight: 700, color: D.txt, marginBottom: 3 }}>Billing & Subscriptions</div>
                  <div style={{ fontSize: 12, color: D.txtMute }}>Plan, payment status, renewal exposure, and seat utilization across all customer organizations.</div>
                </div>

                {/* KPI row */}
                <div style={{ display: "grid", gridTemplateColumns: "repeat(4,1fr)", gap: 14 }}>
                  {kpis.map(c => (
                    <div key={c.label} style={{ background: D.panel, border: `1px solid ${c.alert ? D.bad+"55" : D.border}`, borderRadius: 10, padding: "16px 18px" }}>
                      <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: .6, color: D.txtMute, fontWeight: 600, marginBottom: 8 }}>{c.label}</div>
                      <div style={{ fontSize: 26, fontWeight: 700, color: c.alert ? D.bad : D.txt, fontFamily: "inherit", marginBottom: 4 }}>{c.value}</div>
                      <div style={{ fontSize: 11, color: D.txtDim }}>{c.sub}</div>
                    </div>
                  ))}
                </div>

                {/* Table */}
                <div style={{ background: D.panel, border: `1px solid ${D.border}`, borderRadius: 12, overflow: "hidden" }}>
                  <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                    <thead>
                      <tr style={{ background: D.panel2 }}>
                        {["TENANT","PLAN","BILLING STATUS","SEATS","MRR","RENEWAL / TRIAL END","PAYMENT RISK"].map(h => (
                          <th key={h} style={{ padding: "10px 14px", textAlign: "left", fontSize: 10, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, borderBottom: `1px solid ${D.border}` }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {billingLoading ? (
                        <tr><td colSpan={7} style={{ padding: "32px 14px", textAlign: "center", color: D.txtDim }}>Loading billing data…</td></tr>
                      ) : billingRows.length === 0 ? (
                        <tr><td colSpan={7} style={{ padding: "32px 14px", textAlign: "center", color: D.txtDim }}>No billing subscriptions found.</td></tr>
                      ) : billingRows.map((o, i) => {
                        const sc = statusColor(o.billing_status);
                        const seatsTxt = o.seats_limit ? `${o.seats_used} / ${o.seats_limit}` : `${o.seats_used}`;
                        const isLegacy = o.org_id === "default" || !o.org_id;
                        return (
                          <tr key={`${o.org_id}-${i}`}
                            onClick={() => !isLegacy && openOrgDetail(o.org_id)}
                            style={{ borderTop: i > 0 ? `1px solid ${D.border}` : "none", cursor: isLegacy ? "default" : "pointer" }}>
                            <td style={{ padding: "12px 14px" }}>
                              <div style={{ fontWeight: 600, color: D.txt }}>
                                {isLegacy ? "Platform (legacy)" : o.org_name}
                              </div>
                              <div style={{ fontSize: 10.5, color: D.txtDim, fontFamily: "inherit" }}>
                                {isLegacy ? "Not yet linked to a tenant" : `${o.org_id.slice(0, 18)}…`}
                              </div>
                            </td>
                            <td style={{ padding: "12px 14px", fontWeight: 600, color: D.txt }}>{o.plan_name || "—"}</td>
                            <td style={{ padding: "12px 14px" }}>
                              <span style={{ fontSize: 11, fontWeight: 600, padding: "3px 9px", borderRadius: 20, background: sc.bg, color: sc.color }}>
                                {o.billing_status || "—"}
                              </span>
                            </td>
                            <td style={{ padding: "12px 14px", color: D.txtMute, fontFamily: "inherit", fontSize: 12 }}>{seatsTxt}</td>
                            <td style={{ padding: "12px 14px", color: D.txt, fontFamily: "inherit", fontWeight: 600 }}>{fmtMrr(o.mrr)}</td>
                            <td style={{ padding: "12px 14px", color: D.txtMute, fontSize: 11, fontFamily: "inherit" }}>
                              {o.renewal_date ? new Date(o.renewal_date).toLocaleDateString() : "—"}
                              {o.renewing_soon && <span style={{ marginLeft: 6, fontSize: 10, color: D.warn, fontWeight: 700 }}>soon</span>}
                            </td>
                            <td style={{ padding: "12px 14px", fontSize: 11, color: o.risk_flag ? D.bad : D.txtDim }}>{o.risk_flag || "None"}</td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <div style={{ display: "flex", justifyContent: "space-between", padding: "10px 14px", borderTop: `1px solid ${D.border}`, fontSize: 11, color: D.txtMute }}>
                    <span>{billingRows.length} organization{billingRows.length !== 1 ? "s" : ""} with billing subscriptions</span>
                    <button onClick={loadBilling} style={{ fontSize: 11, color: D.blue, background: "none", border: "none", cursor: "pointer", fontWeight: 600 }}>↻ Refresh</button>
                  </div>
                </div>
              </div>
            );
          })()}

          {/* Create organization tab */}
          {tab === "create_org" && (
            <div style={{ maxWidth: 780, margin: "0 auto", width: "100%" }}>
              {/* Success panel */}
              {createResult && (
                <div style={{
                  background: D.panel, border: `1px solid ${D.border}`, borderRadius: 13,
                  borderLeft: `4px solid ${D.ok}`, padding: "22px 24px", marginBottom: 18,
                }}>
                  {/* Header */}
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 18 }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                      <div style={{
                        width: 38, height: 38, borderRadius: 10, background: D.okSoft,
                        border: `1px solid ${D.ok}44`, display: "flex", alignItems: "center",
                        justifyContent: "center", fontSize: 17, flexShrink: 0,
                      }}>✓</div>
                      <div>
                        <p style={{ margin: 0, fontWeight: 700, fontSize: 15, color: D.ok }}>New Organization Created</p>
                        <p style={{ margin: "3px 0 0", fontSize: 12, color: D.txtMute, lineHeight: 1.5 }}>
                          <b style={{ color: D.txt }}>{createResult.org_name}</b> is now live.
                          The administrator invitation has been created — share the link below to complete onboarding.
                        </p>
                      </div>
                    </div>
                    <Btn D={D} onClick={() => setCreateResult(null)}>Set Up Another Organization</Btn>
                  </div>

                  {/* Email warning (non-fatal) */}
                  {createResult.email_error && (
                    <div style={{
                      display: "flex", gap: 8, padding: "9px 12px", borderRadius: 8, marginBottom: 14,
                      background: D.warnSoft, border: `1px solid rgba(235,167,59,.3)`,
                      fontSize: 12, color: D.txt, lineHeight: 1.5,
                    }}>
                      <span style={{ flexShrink: 0 }}>⚠</span>
                      <span>Invitation email could not be delivered automatically. Please share the invite link below directly with the administrator.</span>
                    </div>
                  )}

                  {/* Key details grid */}
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(170px,1fr))", gap: 12, marginBottom: 16 }}>
                    <KV D={D} label="Organization ID"        value={String(createResult.organization_id)} mono />
                    <KV D={D} label="Administrator Email"    value={createResult.admin_email} />
                    <KV D={D} label="Invitation Email"       value={createResult.email_sent ? "Sent successfully" : "Not sent — share link manually"} />
                    {createResult.invite_expires_at && (
                      <KV D={D} label="Invitation Expires"
                        value={new Date(createResult.invite_expires_at).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })} />
                    )}
                  </div>

                  {/* Seeded counts */}
                  {createResult.seeded && (
                    <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginBottom: 16 }}>
                      {[
                        { label: "Leave Policies", value: createResult.seeded.leave_policies, color: D.ok },
                        { label: "Starter Teams",  value: createResult.seeded.teams,          color: D.teal },
                        { label: "Platform Settings Applied", value: createResult.seeded.app_settings, color: D.blue },
                      ].filter(s => s.value > 0).map(s => (
                        <div key={s.label} style={{
                          padding: "4px 10px", borderRadius: 20, fontSize: 11, fontWeight: 600,
                          background: s.color + "18", border: `1px solid ${s.color}44`, color: s.color,
                        }}>
                          {s.value} {s.label}
                        </div>
                      ))}
                    </div>
                  )}

                  {/* Invite link */}
                  {createResult.invite_link && (
                    <div>
                      <p style={{ margin: "0 0 7px", fontSize: 11, fontWeight: 700, color: D.txtMute, textTransform: "uppercase", letterSpacing: .4 }}>
                        Administrator Invitation Link
                      </p>
                      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                        <input readOnly value={createResult.invite_link}
                          style={{ ...inp, flex: 1, fontFamily: "inherit", fontSize: 11 }} />
                        <Btn D={D} variant="primary" onClick={() => {
                          navigator.clipboard.writeText(createResult.invite_link)
                            .then(() => toast.success("Invite link copied to clipboard"))
                            .catch(() => toast.error("Copy failed — please select and copy manually"));
                        }}>Copy</Btn>
                      </div>
                      <p style={{ margin: "5px 0 0", fontSize: 10.5, color: D.txtDim }}>
                        This link is valid for 24 hours. The administrator must use it to activate their account and complete setup.
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* Form */}
              {!createResult && (
                <div style={{ background: D.panel, border: `1px solid ${D.border}`, borderRadius: 14, overflow: "hidden" }}>

                  {/* Form header */}
                  <div style={{
                    background: `linear-gradient(135deg,${D.panel2},${D.panel3})`,
                    borderBottom: `1px solid ${D.border}`,
                    padding: "20px 26px 18px",
                  }}>
                    <div style={{ display: "flex", alignItems: "flex-start", gap: 14 }}>
                      <div style={{
                        width: 40, height: 40, borderRadius: 11, background: D.blueSoft,
                        border: `1px solid ${D.blue}44`, display: "flex", alignItems: "center",
                        justifyContent: "center", fontSize: 18, flexShrink: 0,
                      }}>🏢</div>
                      <div>
                        <div style={{ fontSize: 15, fontWeight: 700, color: D.txt, marginBottom: 3 }}>Create New Organization</div>
                        <div style={{ fontSize: 12, color: D.txtMute, lineHeight: 1.55, maxWidth: 520 }}>
                          Set up a new customer organization with its core workspace, default policies, teams, and primary administrator. Every setup action is securely recorded for audit and compliance.
                        </div>
                      </div>
                    </div>

                    {/* Step progress dots */}
                    <div style={{ display: "flex", alignItems: "center", gap: 6, marginTop: 16, marginLeft: 54 }}>
                      {["Organization Details", "Primary Administrator", "Initial Setup Options", "Review & Create"].map((s, i) => (
                        <React.Fragment key={i}>
                          <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
                            <div style={{
                              width: 18, height: 18, borderRadius: "50%", fontSize: 10, fontWeight: 700,
                              background: D.blue, color: "#fff",
                              display: "flex", alignItems: "center", justifyContent: "center",
                            }}>{i + 1}</div>
                            <span style={{ fontSize: 10.5, color: D.txtMute, fontWeight: 500, whiteSpace: "nowrap" }}>{s}</span>
                          </div>
                          {i < 3 && <div style={{ flex: 1, height: 1, background: D.border, minWidth: 12 }} />}
                        </React.Fragment>
                      ))}
                    </div>
                  </div>

                  <div style={{ padding: "24px 26px" }}>
                  <form onSubmit={handleCreateOrg}>
                    {createError && (
                      <div style={{
                        padding: "10px 14px", marginBottom: 20, borderRadius: 9,
                        background: D.badSoft, border: `1px solid rgba(240,89,79,.3)`,
                        color: D.bad, fontSize: 13,
                      }}>{createError}</div>
                    )}

                    {/* ── Step 1: Organization Details ───────────────────── */}
                    <div style={{
                      borderLeft: `3px solid ${D.blue}`, paddingLeft: 16, marginBottom: 28,
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: D.txt, marginBottom: 2 }}>Organization Details</div>
                      <div style={{ fontSize: 11.5, color: D.txtMute, marginBottom: 16, lineHeight: 1.5 }}>
                        Core identity of the organization as it will appear across the platform.
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Legal or Organization Name <span style={{ color: D.bad }}>*</span>
                          </label>
                          <input required value={form.name} onChange={e => setField("name", e.target.value)}
                            placeholder="Acme Corp"
                            style={{ ...inp, ...(fieldErrors.name ? { borderColor: D.bad } : {}) }} />
                          {fieldErrors.name && (
                            <div style={{ fontSize: 11, color: D.bad, marginTop: 4, display: "flex", alignItems: "center", gap: 4 }}>
                              ⚠ {fieldErrors.name}
                            </div>
                          )}
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Display Name
                          </label>
                          <input value={form.company_name} onChange={e => setField("company_name", e.target.value)}
                            placeholder="Name shown across the platform" style={inp} />
                          <div style={{ fontSize: 10.5, color: D.txtDim, marginTop: 4, lineHeight: 1.4 }}>
                            Optional. Defaults to the legal name if left blank.
                          </div>
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Workspace URL Identifier
                          </label>
                          <input value={form.slug}
                            onChange={e => { setSlugManual(true); setField("slug", slugify(e.target.value)); }}
                            placeholder="acme-corp"
                            style={{ ...inp, fontFamily: "inherit", ...(fieldErrors.slug ? { borderColor: D.bad } : {}) }} />
                          {fieldErrors.slug
                            ? <div style={{ fontSize: 11, color: D.bad, marginTop: 4 }}>⚠ {fieldErrors.slug}</div>
                            : <div style={{ fontSize: 10.5, color: D.txtDim, marginTop: 4, lineHeight: 1.45 }}>
                                Auto-generated from organization name. Identifies the organization's workspace address.
                              </div>
                          }
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Primary Country
                          </label>
                          <select value={form.country} onChange={e => {
                            const v = e.target.value;
                            setField("country", v);
                            if (v !== "IN") setField("shared_pool_enabled", false);
                          }} style={sel}>
                            <option value="">Select country</option>
                            {COUNTRIES.map(c => <option key={c.code} value={c.code}>{c.name}</option>)}
                          </select>
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>Timezone</label>
                          <select value={form.timezone} onChange={e => setField("timezone", e.target.value)} style={sel}>
                            <option value="">Select timezone</option>
                            {TIMEZONES.map(tz => <option key={tz} value={tz}>{tz}</option>)}
                          </select>
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>Currency</label>
                          <select value={form.currency} onChange={e => setField("currency", e.target.value)} style={sel}>
                            <option value="">Select currency</option>
                            {CURRENCIES.map(c => <option key={c} value={c}>{c}</option>)}
                          </select>
                        </div>
                      </div>
                    </div>

                    {/* ── Step 2: Primary Administrator ──────────────────── */}
                    <div style={{
                      borderLeft: `3px solid ${D.teal}`, paddingLeft: 16, marginBottom: 28,
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: D.txt, marginBottom: 2 }}>Primary Administrator</div>
                      <div style={{ fontSize: 11.5, color: D.txtMute, marginBottom: 16, lineHeight: 1.5 }}>
                        This person will receive the invitation and have full administrator access upon setup.
                      </div>
                      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16 }}>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Administrator Email Address <span style={{ color: D.bad }}>*</span>
                          </label>
                          <input required type="email" value={form.admin_email}
                            onChange={e => setField("admin_email", e.target.value)}
                            placeholder="admin@company.com"
                            style={{ ...inp, ...(fieldErrors.admin_email ? { borderColor: D.bad } : {}) }} />
                          {fieldErrors.admin_email && (
                            <div style={{ fontSize: 11, color: D.bad, marginTop: 4 }}>⚠ {fieldErrors.admin_email}</div>
                          )}
                        </div>
                        <div>
                          <label style={{ display: "block", fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, marginBottom: 6 }}>
                            Administrator Full Name
                          </label>
                          <input value={form.admin_name} onChange={e => setField("admin_name", e.target.value)}
                            placeholder="Jane Smith" style={inp} />
                        </div>
                      </div>
                    </div>

                    {/* ── Step 3: Initial Setup Options ──────────────────── */}
                    <div style={{
                      borderLeft: `3px solid ${D.ok}`, paddingLeft: 16, marginBottom: 28,
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: D.txt, marginBottom: 2 }}>Initial Setup Options</div>
                      <div style={{ fontSize: 11.5, color: D.txtMute, marginBottom: 16, lineHeight: 1.5 }}>
                        Choose which defaults to apply when the organization is first created. These can be changed later.
                      </div>
                      <div style={{ display: "grid", gap: 10 }}>
                        {/* Standard Leave Policies */}
                        <label style={{
                          display: "flex", alignItems: "flex-start", gap: 12, cursor: "pointer",
                          padding: "12px 14px", borderRadius: 10,
                          border: `1px solid ${form.seed_leave_policies ? D.blue : D.border}`,
                          background: form.seed_leave_policies ? D.blueSoft : D.panel3,
                          transition: ".13s",
                        }}>
                          <input type="checkbox" checked={form.seed_leave_policies}
                            onChange={e => setField("seed_leave_policies", e.target.checked)}
                            style={{ marginTop: 2, accentColor: D.blue, width: 14, height: 14, flexShrink: 0 }} />
                          <div>
                            <p style={{ margin: 0, fontWeight: 700, fontSize: 13, color: D.txt }}>Add Standard Leave Policies</p>
                            <p style={{ margin: "3px 0 0", fontSize: 11.5, color: D.txtMute, lineHeight: 1.5 }}>
                              Includes commonly used leave categories such as annual leave, sick leave, maternity leave, and other standard policy types.
                            </p>
                          </div>
                        </label>

                        {/* India Shared Leave Balance — only shown for India */}
                        {form.country === "IN" && (
                          <label style={{
                            display: "flex", alignItems: "flex-start", gap: 12, cursor: "pointer",
                            padding: "12px 14px", borderRadius: 10,
                            border: `1px solid ${form.shared_pool_enabled ? D.teal : D.border}`,
                            background: form.shared_pool_enabled ? D.tealSoft : D.panel3,
                            transition: ".13s",
                          }}>
                            <input type="checkbox" checked={form.shared_pool_enabled}
                              onChange={e => setField("shared_pool_enabled", e.target.checked)}
                              style={{ marginTop: 2, accentColor: D.teal, width: 14, height: 14, flexShrink: 0 }} />
                            <div>
                              <p style={{ margin: 0, fontWeight: 700, fontSize: 13, color: D.txt }}>Use India Shared Leave Balance</p>
                              <p style={{ margin: "3px 0 0", fontSize: 11.5, color: D.txtMute, lineHeight: 1.5 }}>
                                Combines eligible leave types into one shared allowance for India-based leave policy configuration.
                              </p>
                            </div>
                          </label>
                        )}

                        {/* Create Starter Teams */}
                        <label style={{
                          display: "flex", alignItems: "flex-start", gap: 12, cursor: "pointer",
                          padding: "12px 14px", borderRadius: 10,
                          border: `1px solid ${form.seed_default_teams ? D.blue : D.border}`,
                          background: form.seed_default_teams ? D.blueSoft : D.panel3,
                          transition: ".13s",
                        }}>
                          <input type="checkbox" checked={form.seed_default_teams}
                            onChange={e => setField("seed_default_teams", e.target.checked)}
                            style={{ marginTop: 2, accentColor: D.blue, width: 14, height: 14, flexShrink: 0 }} />
                          <div style={{ flex: 1 }}>
                            <p style={{ margin: 0, fontWeight: 700, fontSize: 13, color: D.txt }}>Create Starter Teams</p>
                            <p style={{ margin: "3px 0 0", fontSize: 11.5, color: D.txtMute, lineHeight: 1.5 }}>
                              Automatically adds common departments such as Engineering, HR, Sales, and Operations. You can edit or remove them later.
                            </p>
                            {form.seed_default_teams && (
                              <input value={form.default_teams_raw}
                                onChange={e => setField("default_teams_raw", e.target.value)}
                                placeholder="Engineering, HR, Sales, Operations"
                                style={{ ...inp, marginTop: 10, fontSize: 12.5 }} />
                            )}
                          </div>
                        </label>

                        {/* Leave Accrual Start Period */}
                        <div style={{
                          display: "flex", alignItems: "center", gap: 14,
                          padding: "12px 14px", borderRadius: 10,
                          border: `1px solid ${D.border}`, background: D.panel3,
                        }}>
                          <div style={{ flex: 1 }}>
                            <p style={{ margin: 0, fontWeight: 700, fontSize: 13, color: D.txt }}>Leave Accrual Start Period</p>
                            <p style={{ margin: "3px 0 0", fontSize: 11.5, color: D.txtMute, lineHeight: 1.5 }}>
                              Select how long after joining an employee must wait before leave begins to accrue.
                            </p>
                          </div>
                          <select value={form.leave_waiting_period_months}
                            onChange={e => setField("leave_waiting_period_months", Number(e.target.value))}
                            style={{ ...sel, width: "auto", flexShrink: 0 }}>
                            {[0,1,2,3,4,5,6,9,12].map(n => (
                              <option key={n} value={n}>{n === 0 ? "None (immediate)" : `${n} month${n > 1 ? "s" : ""}`}</option>
                            ))}
                          </select>
                        </div>
                      </div>
                    </div>

                    {/* ── Step 4: Review & Create ─────────────────────────── */}
                    <div style={{
                      borderLeft: `3px solid ${D.warn}`, paddingLeft: 16, marginBottom: 24,
                    }}>
                      <div style={{ fontSize: 13, fontWeight: 700, color: D.txt, marginBottom: 12 }}>Review & Create</div>
                      <div style={{
                        display: "flex", gap: 12, alignItems: "flex-start",
                        background: D.warnSoft, border: `1px solid rgba(235,167,59,.3)`,
                        borderRadius: 10, padding: "12px 15px",
                      }}>
                        <div style={{
                          width: 28, height: 28, borderRadius: 8, background: "rgba(235,167,59,.2)",
                          display: "flex", alignItems: "center", justifyContent: "center", fontSize: 14, flexShrink: 0,
                        }}>⚑</div>
                        <div>
                          <div style={{ fontWeight: 700, fontSize: 13, color: D.txt, marginBottom: 3 }}>Live Production Setup</div>
                          <div style={{ fontSize: 12, color: D.txtMute, lineHeight: 1.55 }}>
                            This organization will be created in the live environment. The setup action will be recorded in the audit trail for compliance and accountability.
                          </div>
                        </div>
                      </div>
                    </div>

                    <style>{`@keyframes sa-spin{to{transform:rotate(360deg)}}`}</style>
                    <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
                      <Btn D={D} onClick={() => { setForm(FORM_EMPTY); setSlugManual(false); setCreateError(""); setFieldErrors({}); }} disabled={createLoading}>
                        Reset Form
                      </Btn>
                      <button type="submit" disabled={createLoading} style={{
                        height: 38, padding: "0 22px", borderRadius: 9, background: D.blue, border: "none",
                        color: "#fff", fontWeight: 700, fontSize: 13, cursor: createLoading ? "not-allowed" : "pointer",
                        opacity: createLoading ? .8 : 1, fontFamily: "inherit", minWidth: 230,
                        display: "flex", alignItems: "center", justifyContent: "center", gap: 8,
                        transition: ".15s",
                      }}>
                        {createLoading ? (
                          <>
                            <span style={{
                              display: "inline-block", width: 13, height: 13, flexShrink: 0,
                              border: "2px solid rgba(255,255,255,.35)", borderTopColor: "#fff",
                              borderRadius: "50%", animation: "sa-spin .7s linear infinite",
                            }} />
                            Creating organization…
                          </>
                        ) : "Create New Organization"}
                      </button>
                    </div>
                  </form>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* ── Org detail drawer ─────────────────────────────────────────── */}
      {detail && (
        <OrgDrawer
          detail={detail}
          detailLoading={detailLoading}
          onClose={() => setDetail(null)}
          onSuspend={(id, name) => { setDetail(null); setStateModal({ orgId: id, name, action: "suspend" }); }}
          onRestore={(id, name) => { setDetail(null); setStateModal({ orgId: id, name, action: "restore" }); }}
          onDelete={(id, name) => { setDetail(null); setDeleteTyped(""); setDeleteModal({ orgId: id, name }); }}
          onImpersonate={(u, orgName) => { setImpModal({ user: u, orgName }); }}
          D={D}
          isDark={isDark}
        />
      )}

      {/* ── Suspend / restore modal ────────────────────────────────────── */}
      {stateModal && (
        <Modal
          D={D}
          title={stateModal.action === "suspend" ? `Suspend ${stateModal.name}` : `Reactivate ${stateModal.name}`}
          icon={stateModal.action === "suspend" ? "!" : "↻"}
          iconCls={stateModal.action === "suspend" ? "warn" : "info"}
          onClose={() => setStateModal(null)}
          footer={<>
            <Btn D={D} onClick={() => setStateModal(null)}>Cancel</Btn>
            <Btn D={D} variant={stateModal.action === "suspend" ? "danger" : "primary"} onClick={performStateChange} disabled={stateWorking}>
              {stateWorking ? "Working…" : stateModal.action === "suspend" ? "Suspend" : "Reactivate"}
            </Btn>
          </>}
        >
          <p style={{ margin: "0 0 12px", color: D.txtMute, fontSize: 13.5, lineHeight: 1.5 }}>
            {stateModal.action === "suspend"
              ? "Everyone in this organization is signed out and blocked from Temveris immediately — web, API and desktop tracker. No data is deleted, and reactivating restores full access."
              : "This restores tenant access. Everyone will need to sign in again. Confirm billing or security concerns have been resolved."}
          </p>
          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, margin: "13px 0 6px" }}>
            Reason {stateModal.action === "suspend" && <span style={{ color: D.bad }}>required</span>}
          </div>
          <textarea value={stateReason} onChange={e => setStateReason(e.target.value)}
            placeholder="e.g. Billing failure × 3 — restrict pending resolution"
            rows={3}
            style={{ ...inp, resize: "vertical", minHeight: 66 }} />
          <div style={{
            display: "flex", gap: 8, background: D.panel3, border: `1px solid ${D.border}`,
            borderRadius: 9, padding: "9px 11px", marginTop: 13, fontSize: 10.5, color: D.txt, lineHeight: 1.5,
          }}>
            <span style={{ fontWeight: 700 }}>♦</span>
            <span>This action is logged to the immutable audit trail with your identity, IP, timestamp, and reason.</span>
          </div>
        </Modal>
      )}

      {/* ── Delete organization modal ──────────────────────────────────── */}
      {deleteModal && (
        <Modal
          D={D}
          title="Delete Organization?"
          icon="⚠"
          iconCls="warn"
          onClose={() => { setDeleteModal(null); setDeleteTyped(""); }}
          footer={<>
            <Btn D={D} onClick={() => { setDeleteModal(null); setDeleteTyped(""); }}>Cancel</Btn>
            <Btn D={D} variant="danger" onClick={performDelete} disabled={!deleteArmed || deleteWorking}>
              {deleteWorking ? "Deleting…" : "Delete Organization"}
            </Btn>
          </>}
        >
          <p style={{ margin: "0 0 12px", color: D.txt, fontSize: 13.5, lineHeight: 1.5 }}>
            This permanently removes the organization and its associated data —
            users, teams, attendance, activity, screenshots, leave, payroll,
            settings and audit history. <strong>This action cannot be undone.</strong>
          </p>

          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtDim, fontWeight: 600, margin: "13px 0 5px" }}>
            Organization
          </div>
          <div style={{
            background: D.panel3, border: `1px solid ${D.border}`, borderRadius: 8,
            padding: "9px 11px", fontSize: 13.5, fontWeight: 700, color: D.txt, userSelect: "all",
          }}>{deleteModal.name}</div>

          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtDim, fontWeight: 600, margin: "13px 0 5px" }}>
            Type the organization name to confirm
          </div>
          <input
            value={deleteTyped}
            onChange={e => setDeleteTyped(e.target.value)}
            placeholder={deleteModal.name}
            aria-label="Type the organization name to confirm"
            autoFocus
            style={{
              ...inp,
              borderColor: deleteTyped && !deleteArmed ? D.bad : undefined,
            }}
          />
          {deleteTyped && !deleteArmed && (
            <div style={{ fontSize: 11, color: D.bad, marginTop: 5 }}>
              The name does not match yet.
            </div>
          )}

          <div style={{
            display: "flex", gap: 8, background: D.panel3, border: `1px solid ${D.border}`,
            borderRadius: 9, padding: "9px 11px", marginTop: 13, fontSize: 10.5, color: D.txt, lineHeight: 1.5,
          }}>
            <span style={{ fontWeight: 700 }}>♦</span>
            <span>
              Deletion runs as a single transaction — if any step fails, nothing is
              removed. A platform-level audit record of this deletion is retained.
            </span>
          </div>
        </Modal>
      )}

      {/* ── Impersonate modal ──────────────────────────────────────────── */}
      {impModal && (
        <Modal
          D={D}
          title={`Impersonate Admin · ${impModal.orgName}`}
          icon="⚠"
          iconCls="warn"
          onClose={() => setImpModal(null)}
          footer={<>
            <Btn D={D} onClick={() => setImpModal(null)}>Cancel</Btn>
            <Btn D={D} variant="primary" onClick={performImpersonate} disabled={impWorking || !impReason.trim()}>
              {impWorking ? "Signing in…" : "Start Logged Session"}
            </Btn>
          </>}
        >
          <p style={{ margin: "0 0 12px", color: D.txtMute, fontSize: 13, lineHeight: 1.5 }}>
            You will enter <b style={{ color: D.txt }}>{impModal.user.email}</b> as an administrator. The session is time-limited and fully logged.
          </p>
          <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: .5, color: D.txtMute, fontWeight: 600, margin: "13px 0 6px" }}>
            Reason for impersonation <span style={{ color: D.bad }}>required</span>
          </div>
          <textarea value={impReason} onChange={e => setImpReason(e.target.value)}
            placeholder="e.g. Reproduce timesheet approval error — ticket SUP-4471"
            rows={3}
            style={{ ...inp, resize: "vertical", minHeight: 66 }} />
          <div style={{
            display: "flex", gap: 8, background: D.warnSoft, border: `1px solid rgba(235,167,59,.28)`,
            borderRadius: 9, padding: "9px 11px", marginTop: 13, fontSize: 10.5, color: D.txt, lineHeight: 1.5,
          }}>
            <span style={{ fontWeight: 700, flexShrink: 0 }}>⚑</span>
            <span><b>Worker-level sensitive content</b> remains hidden unless explicitly permissioned. A visible "Super Admin session" marker is shown to the tenant throughout.</span>
          </div>
        </Modal>
      )}
    </div>
  );
}
