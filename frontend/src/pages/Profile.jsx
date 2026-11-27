/**
 * Profile.jsx — fully dynamic employee Profile module.
 *
 * One page, six sections:
 *   1. Personal Info        – name, phone, designation, profile picture
 *   2. Work Preferences     – timezone, working hours, working days, availability
 *   3. Account Security     – change password + active sessions
 *   4. Productivity Summary – today's working / active / idle time + top apps
 *   5. Notification Prefs   – per-channel toggles for schedules, summaries, etc.
 *   6. Access History       – recent logins + security events
 *
 * Backend: all reads/writes go through the /profile/* router.
 * Layout: admins/HR get the persistent AdminSidebar; employees get a
 * lightweight back-button header so the page stays scoped to one task.
 */

import React, { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";
import toast from "react-hot-toast";
import API, { saveErrorMessage } from "../api/axios";
import AdminSidebar from "../components/AdminSidebar";
import TopNavbar from "../components/TopNavbar";
import { useLanguage } from "../context/LanguageContext";
import { useTheme } from "../context/ThemeContext";
import { useAuth } from "../context/AuthContext";

// Module-level context so sub-components get C without prop-drilling.
const CCtx = React.createContext(null);
const useC = () => React.useContext(CCtx);

// ─── helpers ─────────────────────────────────────────────────────────────────
const initials = (name) =>
  (name || "").split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2) || "U";

const fmtHM = (sec) => {
  if (!sec || sec < 0) sec = 0;
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  return `${h}h ${String(m).padStart(2, "0")}m`;
};

const fmtRel = (iso) => {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "—";
  const diff = Math.max(0, Date.now() - t);
  const sec = Math.floor(diff / 1000);
  if (sec < 60)        return "just now";
  if (sec < 3600)      return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400)     return `${Math.floor(sec / 3600)}h ago`;
  if (sec < 86400 * 7) return `${Math.floor(sec / 86400)}d ago`;
  try { return new Date(iso).toLocaleDateString(); } catch { return "—"; }
};

const fmtDateTime = (iso) => {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString(undefined, {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return "—"; }
};

// Compress an uploaded image to a 256-px JPEG data URL — same approach
// as AccountSettings so the profile_picture stays well under the 2 MB cap.
const fileToCompactDataURL = (file, maxSide = 256, quality = 0.85) =>
  new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width  * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/jpeg", quality));
      };
      img.onerror = reject;
      img.src = reader.result;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });

const DAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

// Displays a policy value the employee cannot edit. Deliberately not a
// disabled <input>: this is not a form control that happens to be locked, it
// is a read-out of what the organization configured.
const ReadOnly = ({ value }) => (
  <div style={{ padding: "10px 12px", borderRadius: 8, background: "rgba(127,127,127,.08)",
                fontSize: 14, fontWeight: 600, minHeight: 20 }}>
    {value || "—"}
  </div>
);

// ─── tiny inline SVG icon set ───────────────────────────────────────────────
const ICON = {
  user:       "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2 M12 11a4 4 0 100-8 4 4 0 000 8",
  briefcase:  "M20 7H4a2 2 0 00-2 2v10a2 2 0 002 2h16a2 2 0 002-2V9a2 2 0 00-2-2z M16 7V5a2 2 0 00-2-2h-4a2 2 0 00-2 2v2",
  shield:     "M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z M9 12l2 2 4-4",
  bell:       "M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9 M13.73 21a2 2 0 01-3.46 0",
  clock:      "M12 22a10 10 0 100-20 10 10 0 000 20z M12 6v6l4 2",
  history:    "M3 3v6h6 M3.51 9a9 9 0 1 0 2.13-9.36L1 9 M12 7v5l4 2",
  monitor:    "M2 3h20a1 1 0 011 1v13a1 1 0 01-1 1H2a1 1 0 01-1-1V4a1 1 0 011-1z M8 21h8 M12 17v4",
  smartphone: "M5 2h14a2 2 0 012 2v16a2 2 0 01-2 2H5a2 2 0 01-2-2V4a2 2 0 012-2z M12 18h.01",
  trash:      "M3 6h18 M8 6V4a2 2 0 012-2h4a2 2 0 012 2v2 M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6",
  back:       "M19 12H5 M12 19l-7-7 7-7",
  check:      "M20 6L9 17l-5-5",
  info:       "M12 22a10 10 0 100-20 10 10 0 000 20z M12 8h.01 M12 12v4",
  trending:   "M23 6l-9.5 9.5-5-5L1 18 M17 6h6v6",
  globe:      "M12 2a10 10 0 100 20A10 10 0 0012 2z M2 12h20 M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z",
  activity:   "M22 12h-4l-3 9L9 3l-3 9H2",
  pause:      "M6 4h4v16H6z M14 4h4v16h-4z",
  bar:        "M12 20V10 M18 20V4 M6 20v-6",
};

const Icon = ({ n, s = 16, c = "#475467", w = 2 }) => (
  <svg width={s} height={s} viewBox="0 0 24 24" fill="none"
    stroke={c} strokeWidth={w} strokeLinecap="round" strokeLinejoin="round"
    style={{ flexShrink: 0, display: "block" }}>
    {(ICON[n] || "").split(" M").map((seg, i) => (
      <path key={i} d={i === 0 ? seg : "M" + seg} />
    ))}
  </svg>
);

// ─── shared keyframe + animation registry ───────────────────────────────────
// One <style> block at module mount instead of per-component duplicates so
// the keyframes (spin / fade / pulse / ring) are defined once and every
// widget below can reference them by name.
const ProfileFx = () => (
  <style>{`
    @keyframes zt-spin { to { transform: rotate(360deg) } }
    @keyframes zt-fade-up {
      0%   { opacity: 0; transform: translateY(8px) }
      100% { opacity: 1; transform: translateY(0)   }
    }
    @keyframes zt-pulse-ring {
      0%   { box-shadow: 0 0 0 0   rgba(68,185,123,.55) }
      70%  { box-shadow: 0 0 0 12px rgba(68,185,123,0)  }
      100% { box-shadow: 0 0 0 0   rgba(68,185,123,0)  }
    }
    @keyframes zt-progress-shimmer {
      0%   { background-position: -200% 0 }
      100% { background-position:  200% 0 }
    }
    /* Uiverse-style additions ───────────────────────────────────────── */
    @keyframes zt-orb-float-a {
      0%,100% { transform: translate3d(0,0,0)        scale(1)   }
      50%     { transform: translate3d(18px,-12px,0) scale(1.08) }
    }
    @keyframes zt-orb-float-b {
      0%,100% { transform: translate3d(0,0,0)         scale(1)   }
      50%     { transform: translate3d(-22px,14px,0)  scale(1.12) }
    }
    /* Rotating conic gradient — used as an animated ring around the avatar.
       Pure CSS, GPU-cheap, and respects the brand palette. */
    @keyframes zt-ring-rotate { to { transform: rotate(360deg) } }
    /* Click ripple — kicked off in JS by adding a span with this class. */
    @keyframes zt-ripple {
      0%   { transform: scale(0);   opacity: .55 }
      100% { transform: scale(2.5); opacity: 0   }
    }
    .zt-fade-up { animation: zt-fade-up .35s cubic-bezier(.22,1,.36,1) both }
    .zt-ripple-host { position: relative; overflow: hidden; isolation: isolate; }
    .zt-ripple-span {
      position: absolute; border-radius: 50%;
      width: 14px; height: 14px;
      background: rgba(255,255,255,.55);
      pointer-events: none;
      animation: zt-ripple .6s ease-out forwards;
    }
    /* Stat tile — purely structural class. All hover styling now lives on
       the element's inline transitions so we get a consistent, restrained
       motion language across the page (no rotating rings). */
    .zt-stat-tile { position: relative }
  `}</style>
);

// ─── reusable widgets ───────────────────────────────────────────────────────
const Card = ({ title, icon, action, children, padding = 24 }) => {
  const C = useC();
  const [hover, setHover] = useState(false);
  return (
    <section
      className="zt-fade-up"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        background: C.surface, borderRadius: C.r,
        border: `1px solid ${hover ? C.brandBdr || C.brand : C.border}`,
        // Subtle hover lift — gives the surface a tactile feel without
        // making the page jump on every cursor wiggle.
        boxShadow: hover
          ? "0 8px 24px rgba(16,24,40,0.08), 0 1px 2px rgba(16,24,40,0.04)"
          : "0 1px 2px rgba(16,24,40,0.04)",
        marginBottom: 20, overflow: "hidden",
        transition: "box-shadow .25s ease, border-color .25s ease, transform .25s ease",
        transform: hover ? "translateY(-1px)" : "translateY(0)",
      }}>
      {(title || icon) && (
        <header style={{
          padding: "16px 24px", borderBottom: `1px solid ${C.borderLight}`,
          display: "flex", alignItems: "center", gap: 10,
        }}>
          {icon && (
            <span style={{
              width: 32, height: 32, borderRadius: 8,
              background: hover ? C.brand : C.brandBg,
              display: "inline-flex",
              alignItems: "center", justifyContent: "center",
              transition: "background .2s ease, transform .2s ease",
            }}>
              <Icon n={icon} s={16} c={hover ? "#fff" : C.brand} />
            </span>
          )}
          <h2 style={{ flex: 1, fontSize: 15, fontWeight: 700, color: C.text, margin: 0 }}>
            {title}
          </h2>
          {action}
        </header>
      )}
      <div style={{ padding }}>{children}</div>
    </section>
  );
};

const Field = ({ label, helper, children }) => {
  const C = useC();
  return (
    <div style={{ marginBottom: 16 }}>
      <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 6 }}>
        {label}
      </label>
      {children}
      {helper && <p style={{ fontSize: 11, color: C.textLight, marginTop: 4 }}>{helper}</p>}
    </div>
  );
};

const Input = ({ value, onChange, type = "text", placeholder, disabled, autoComplete }) => {
  const C = useC();
  const [showPw, setShowPw] = useState(false);
  const isPw = type === "password";
  const input = (
    <input
      type={isPw && showPw ? "text" : type}
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      placeholder={placeholder}
      disabled={disabled}
      autoComplete={autoComplete}
      style={{
        width: "100%", padding: "10px 14px", paddingRight: isPw ? 42 : 14, borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: disabled ? C.textSub : C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box",
        // Smooth, brand-tinted focus ring — the previous step change felt
        // abrupt; this animates the border + ring + lift in unison.
        transition: "border-color .18s ease, box-shadow .18s ease, background .18s ease, transform .18s ease",
        boxShadow: "0 0 0 0 rgba(17,73,106,0)",
      }}
      onFocus={(e) => {
        if (disabled) return;
        e.target.style.borderColor = C.brand;
        e.target.style.background  = C.surface;
        e.target.style.boxShadow   = `0 0 0 3px ${C.brandBg}`;
      }}
      onBlur={(e) => {
        e.target.style.borderColor = C.border;
        e.target.style.background  = disabled ? C.borderLight : C.bg;
        e.target.style.boxShadow   = "0 0 0 0 rgba(17,73,106,0)";
      }}
    />
  );
  if (!isPw) return input;
  return (
    <div style={{ position: "relative" }}>
      {input}
      <button
        type="button"
        onClick={() => setShowPw((s) => !s)}
        aria-label={showPw ? "Hide password" : "Show password"}
        style={{
          position: "absolute", right: 6, top: "50%", transform: "translateY(-50%)",
          display: "flex", alignItems: "center", justifyContent: "center",
          background: "none", border: "none", cursor: "pointer", padding: 6,
          color: C.textSub,
        }}
      >
        {showPw ? <EyeOff size={18} /> : <Eye size={18} />}
      </button>
    </div>
  );
};

const Select = ({ value, onChange, options, disabled }) => {
  const C = useC();
  return (
    <select
      value={value ?? ""}
      onChange={(e) => onChange?.(e.target.value)}
      disabled={disabled}
      style={{
        width: "100%", padding: "10px 14px", borderRadius: C.rSm,
        border: `1px solid ${C.border}`, background: disabled ? C.borderLight : C.bg,
        fontSize: 14, color: C.text, fontFamily: "inherit",
        outline: "none", boxSizing: "border-box", appearance: "auto",
      }}>
      {options.map((o) =>
        typeof o === "string"
          ? <option key={o} value={o}>{o}</option>
          : <option key={o.value} value={o.value}>{o.label}</option>
      )}
    </select>
  );
};

const Toggle = ({ on, onChange, disabled }) => {
  const C = useC();
  return (
    <button
      type="button"
      onClick={() => !disabled && onChange?.(!on)}
      disabled={disabled}
      style={{
        width: 40, height: 22, borderRadius: 12, border: "none",
        cursor: disabled ? "not-allowed" : "pointer",
        background: on ? C.brand : C.textLight, position: "relative",
        transition: "background .18s ease", flexShrink: 0,
      }}>
      <span style={{
        position: "absolute", top: 2, left: on ? 20 : 2,
        width: 18, height: 18, background: "#fff", borderRadius: "50%",
        transition: "left .18s ease", boxShadow: "0 1px 3px rgba(0,0,0,0.2)",
      }} />
    </button>
  );
};

const Button = ({ children, variant = "primary", onClick, disabled, type = "button", style }) => {
  const C = useC();
  const [hover, setHover] = useState(false);
  const [pressed, setPressed] = useState(false);
  const variants = {
    // Primary swaps the flat brand fill for a brand → green gradient so
    // the CTA reads as the most important action on the page.
    primary:   {
      bg: disabled ? C.textLight : `linear-gradient(135deg, ${C.brand} 0%, #44B97B 100%)`,
      color: "#fff", border: "none",
    },
    secondary: { bg: C.surface, color: C.text,     border: `1px solid ${C.border}` },
    danger:    { bg: disabled ? "#fbb" : `linear-gradient(135deg, ${C.danger} 0%, #B91C1C 100%)`,
                 color: "#fff", border: "none" },
    ghost:     { bg: "transparent", color: C.brand, border: "none" },
  };
  const v = variants[variant] || variants.primary;
  // Detect the "Saving…" pattern so we can swap in a spinner without
  // forcing every caller to pass a loading prop.
  const isSaving = typeof children === "string" && /…|\.\.\.$/.test(children);

  // Spawn a short-lived ripple span at the click point. The .zt-ripple-host
  // class clips overflow + isolates the stacking context. Auto-cleaned
  // after 600ms (matches the keyframe duration).
  const spawnRipple = (e) => {
    if (disabled) return;
    try {
      const btn = e.currentTarget;
      const rect = btn.getBoundingClientRect();
      const span = document.createElement("span");
      span.className = "zt-ripple-span";
      span.style.left = (e.clientX - rect.left - 7) + "px";
      span.style.top  = (e.clientY - rect.top  - 7) + "px";
      btn.appendChild(span);
      setTimeout(() => span.remove(), 620);
    } catch { /* no-op */ }
  };

  return (
    <button
      type={type}
      onClick={(e) => { spawnRipple(e); onClick?.(e); }}
      disabled={disabled}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => { setHover(false); setPressed(false); }}
      onMouseDown={() => setPressed(true)}
      onMouseUp={() => setPressed(false)}
      className="zt-ripple-host"
      style={{
        padding: "10px 18px", borderRadius: C.rSm,
        background: v.bg, color: v.color, border: v.border,
        fontWeight: 600, fontSize: 13,
        cursor: disabled ? "default" : "pointer",
        fontFamily: "inherit", whiteSpace: "nowrap",
        display: "inline-flex", alignItems: "center", gap: 8,
        // Hover lift on primary; secondary/ghost only get a subtle bg tint.
        transform: !disabled && pressed ? "translateY(1px)" :
                   !disabled && hover   ? "translateY(-1px)" : "translateY(0)",
        boxShadow: variant === "primary" && !disabled
          ? (hover ? "0 8px 20px rgba(17,73,106,0.35)" : "0 2px 6px rgba(17,73,106,0.22)")
          : "none",
        transition: "transform .15s ease, box-shadow .15s ease, filter .15s ease",
        filter: hover && !disabled ? "brightness(1.05)" : "brightness(1)",
        ...style,
      }}>
      {/* Ambient sheen sweeping across primary buttons — gives a subtle
          "live" indicator without distracting from the label. */}
      {/* Ambient shine removed — a constantly-animating sweep on every
          primary button reads as flashy in business contexts. The click
          ripple below remains for tactile feedback. */}
      {isSaving && (
        <span style={{
          width: 12, height: 12, borderRadius: "50%",
          border: `2px solid ${variant === "primary" ? "rgba(255,255,255,.35)" : C.borderLight}`,
          borderTopColor: variant === "primary" ? "#fff" : C.brand,
          animation: "zt-spin .8s linear infinite",
          position: "relative", zIndex: 1,
        }} />
      )}
      <span style={{ position: "relative", zIndex: 1 }}>{children}</span>
    </button>
  );
};

const Stat = ({ label, value, accent, sub, icon }) => {
  const C = useC();
  const [hover, setHover] = useState(false);
  const col = accent ?? C.brand;
  // Glassmorphic surface — frosted base + animated conic gradient border
  // on hover (the .zt-stat-tile class provides the rotating ring). The
  // colored hover tint multiplies on top so the accent reads through.
  return (
    <div
      className="zt-stat-tile"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        flex: 1, minWidth: 150,
        padding: "14px 16px",
        borderRadius: 12,
        // Clean opaque surface on idle; a soft tint of the accent on hover.
        // No glassmorphism or rotating gradient — restrained business UI.
        background: hover ? `${col}0E` : C.surface,
        border: `1px solid ${hover ? `${col}55` : C.border}`,
        transition: "background .2s ease, border-color .2s ease, transform .2s ease, box-shadow .2s ease",
        transform: hover ? "translateY(-2px)" : "translateY(0)",
        boxShadow: hover
          ? `0 4px 12px rgba(16,24,40,0.06)`
          : "0 1px 2px rgba(16,24,40,0.04)",
        cursor: "default",
      }}>
      <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 4 }}>
        {icon && (
          <span style={{
            width: 20, height: 20, borderRadius: 5,
            display: "inline-flex", alignItems: "center", justifyContent: "center",
            background: hover ? col : `${col}1F`,
            border: `1px solid ${hover ? col : `${col}33`}`,
            transition: "background .2s ease, border-color .2s ease",
          }}>
            <Icon n={icon} s={11} c={hover ? "#fff" : col} />
          </span>
        )}
        <p style={{ fontSize: 11, color: C.textLight, fontWeight: 700, letterSpacing: 0.5,
                    textTransform: "uppercase", margin: 0 }}>{label}</p>
      </div>
      <p style={{
        fontSize: 22, fontWeight: 800, margin: 0, lineHeight: 1.1,
        fontVariantNumeric: "tabular-nums",
        color: col,
      }}>
        {value}
      </p>
      {sub && <p style={{ fontSize: 12, color: C.textSub, margin: "4px 0 0" }}>{sub}</p>}
    </div>
  );
};

const Tab = ({ label, active, onClick, badge }) => {
  const C = useC();
  const [hover, setHover] = useState(false);
  return (
    <button
      type="button"
      onClick={onClick}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      style={{
        position: "relative",
        padding: "10px 16px", border: "none",
        // Clean text-only tabs — colour shift on hover, solid underline on
        // active. No gradient pills or background fills. Reads as a stable
        // navigation row, not a flashy widget.
        background: "transparent",
        color: active ? C.brand : hover ? C.text : C.textSub,
        fontWeight: active ? 700 : 600,
        fontSize: 13, cursor: "pointer", fontFamily: "inherit",
        display: "flex", alignItems: "center", gap: 7,
        transition: "color .2s ease",
      }}>
      {label}
      {badge != null && (
        <span style={{
          fontSize: 10, padding: "2px 8px", borderRadius: 999,
          background: active ? C.brand : C.borderLight,
          color: active ? "#fff" : C.textSub, fontWeight: 700,
          letterSpacing: 0.2,
          transition: "background .2s ease, color .2s ease",
        }}>{badge}</span>
      )}
      {/* Underline indicator — a single 2 px line that fades in. Restrained,
          unmistakable, and consistent with the rest of the page. */}
      <span style={{
        position: "absolute", left: 12, right: 12, bottom: -1,
        height: 2, borderRadius: 2,
        background: C.brand,
        opacity: active ? 1 : 0,
        transition: "opacity .2s ease",
      }} />
    </button>
  );
};

// ═════════════════════════════════════════════════════════════════════════════
// EMBEDDABLE CONTENT  (used both standalone and inside the Employee Dashboard)
// ═════════════════════════════════════════════════════════════════════════════
export function ProfileContent({ embedded = false, maxWidth = 980, userId = null, onBack = null }) {
  const { T } = useLanguage();
  const { C } = useTheme();
  const { refreshUser, updateUserLocal, user: authUser } = useAuth();
  const fileRef = useRef(null);

  const role = (authUser?.role || localStorage.getItem("role") || "").toLowerCase();

  // View mode:
  //   "self"     → the user's own profile (editable, full surface)
  //   "other"    → directory view of a teammate (read-only, public fields)
  //   "preview"  → owner is previewing the public read-only view
  //
  // Driven by the `userId` prop. When a teammate's id is passed, the page
  // pulls /profile/users/{id} (public-safe payload) and disables every
  // mutating control so a worker can't edit someone else's profile.
  const myId = authUser?.id ?? Number(localStorage.getItem("user_id")) ?? null;
  const isOtherUser = !!(userId && myId && Number(userId) !== Number(myId));
  const [previewPublic, setPreviewPublic] = useState(false);
  const viewMode  = isOtherUser ? "other" : (previewPublic ? "preview" : "self");
  const readOnly  = viewMode !== "self";

  // ── data state ───────────────────────────────────────────────────────────
  const [loading,    setLoading]    = useState(true);
  const [profile,    setProfile]    = useState(null);    // /profile/me
  const [productivity, setProd]     = useState(null);    // /profile/productivity-summary
  const [sessions,   setSessions]   = useState([]);      // /profile/sessions
  const [history,    setHistory]    = useState([]);      // /profile/access-history

  const [activeTab,  setActiveTab]  = useState("overview"); // overview | work | security | notifications | history

  // ── editable form state ──────────────────────────────────────────────────
  const [name,         setName]        = useState("");
  const [phone,        setPhone]       = useState("");
  const [designation,  setDesignation] = useState("");
  const [department,   setDepartment]  = useState("");
  const [picture,      setPicture]     = useState(null);
  const [savingProfile, setSavingProfile] = useState(false);

  // Work schedule — read-only mirror of the resolved org/team/employee policy.
  // No local defaults: an empty page is honest, an invented "09:00 / 8h" is not.
  const [timezone,    setTimezone]    = useState("");
  const [workStart,   setWorkStart]   = useState("");
  const [workEnd,     setWorkEnd]     = useState("");
  const [workDays,    setWorkDays]    = useState([]);
  const [targetHours, setTargetHours] = useState(null);
  const [available,   setAvailable]   = useState(true);
  const [awayMessage, setAwayMessage] = useState("");
  const [savingPrefs, setSavingPrefs] = useState(false);

  // Notifications
  const [notif,        setNotif]      = useState({});
  const [savingNotif,  setSavingNotif] = useState(false);

  // Password
  const [pwCurrent, setPwCurrent] = useState("");
  const [pwNew,     setPwNew]     = useState("");
  const [pwConfirm, setPwConfirm] = useState("");
  const [savingPw,  setSavingPw]  = useState(false);

  // ── initial load ─────────────────────────────────────────────────────────
  // Owner view fetches the full bundle (/me + productivity + sessions +
  // history). Teammate view fetches only the public-safe /users/{id}
  // projection — sessions, access history, and productivity stay private.
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    (async () => {
      try {
        const owner = !isOtherUser;
        const requests = owner
          ? [
              API.get("/profile/me"),
              API.get("/profile/productivity-summary"),
              API.get("/profile/sessions"),
              API.get("/profile/access-history"),
            ]
          : [API.get(`/profile/users/${userId}`)];

        const settled = await Promise.allSettled(requests);
        if (cancelled) return;

        const meRes = settled[0];
        if (meRes.status === "fulfilled") {
          const d = meRes.value.data;
          setProfile(d);
          setName(d.name || "");
          setPhone(d.phone || "");
          setDesignation(d.designation || "");
          setDepartment(d.department || "");
          setPicture(d.profile_picture || null);
          const p = d.preferences || {};
          // Schedule values are the RESOLVED org/team/employee policy the
          // backend sends back — displayed, never edited here.
          setTimezone(p.timezone || "");
          setWorkStart(p.work_start_time || "");
          setWorkEnd(p.work_end_time || "");
          setWorkDays(Array.isArray(p.work_days) ? p.work_days : []);
          setTargetHours(p.daily_target_hours ?? null);
          setAvailable(p.is_available !== false);
          setAwayMessage(p.away_message || "");
          setNotif(p.notifications || {});
        } else {
          toast.error(T.couldntLoadProfile || "Couldn't load profile");
        }

        if (owner) {
          if (settled[1]?.status === "fulfilled") setProd(settled[1].value.data);
          if (settled[2]?.status === "fulfilled") setSessions(settled[2].value.data || []);
          if (settled[3]?.status === "fulfilled") setHistory(settled[3].value.data || []);
        } else {
          // Teammate view: explicitly clear any prior owner-state remnants.
          setProd(null);
          setSessions([]);
          setHistory([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId, isOtherUser]);

  // ── save handlers ────────────────────────────────────────────────────────
  const saveProfile = async () => {
    const trimmed = name.trim();
    if (!trimmed) { toast.error(T.profileNameEmpty || "Name can't be empty"); return; }
    setSavingProfile(true);
    try {
      const { data } = await API.patch("/profile/me", {
        name:            trimmed,
        phone:           phone.trim(),
        designation:     designation.trim(),
        department:      department.trim(),
        profile_picture: picture,
      });
      // Optimistic UI: update the AuthContext immediately so the topbar
      // avatar/name flips before /auth/me round-trips. Then ask AuthContext
      // to re-fetch authoritative data so any derived fields (role, email,
      // designation) stay in sync.
      updateUserLocal({
        name: data.name || trimmed,
        profile_picture: data.profile_picture ?? null,
      });
      setProfile((prev) => prev ? { ...prev, ...data } : prev);
      try { await refreshUser(); } catch {}
      toast.success("Profile updated");
    } catch (err) {
      toast.error(saveErrorMessage(err, T.profileCouldntSave || "Couldn't save — try again"));
    } finally {
      setSavingProfile(false);
    }
  };

  const handlePickFile = () => fileRef.current?.click();
  const handleFileChange = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)$/i.test(file.type)) {
      toast.error(T.profileUploadImageType || "Please upload a PNG, JPG, or WebP image");
      return;
    }
    try {
      setPicture(await fileToCompactDataURL(file));
    } catch {
      toast.error(T.profileCouldntReadImage || "Couldn't read that image — try another file");
    }
  };

  const savePrefs = async () => {
    setSavingPrefs(true);
    try {
      // Availability only. Shift, working days and daily target are workforce
      // policy — the backend ignores them on this endpoint by design.
      await API.put("/profile/preferences", {
        is_available:       available,
        away_message:       awayMessage,
      });
      toast.success(T.profilePrefsSaved || "Work preferences saved");
    } catch (err) {
      toast.error(err?.response?.data?.detail || T.profileCouldntSavePrefs || "Couldn't save preferences");
    } finally {
      setSavingPrefs(false);
    }
  };

  const saveNotifications = async (next) => {
    const merged = { ...notif, ...next };
    setNotif(merged);
    setSavingNotif(true);
    try {
      await API.put("/profile/notifications", next);
      // success is implicit — saving toggles individually feels less noisy
    } catch (err) {
      // roll back the optimistic flip
      setNotif(notif);
      toast.error(err?.response?.data?.detail || T.profileCouldntUpdateNotif || "Couldn't update notification");
    } finally {
      setSavingNotif(false);
    }
  };

  const changePassword = async () => {
    if (pwNew.length < 8) { toast.error(T.profilePwMinLength || "New password must be at least 8 characters"); return; }
    if (pwNew !== pwConfirm) { toast.error(T.profilePwMismatch || "Passwords don't match"); return; }
    if (pwNew === pwCurrent) { toast.error(T.profilePwMustDiffer || "New password must differ from current"); return; }
    setSavingPw(true);
    try {
      const { data } = await API.post("/profile/password", {
        current_password: pwCurrent,
        new_password:     pwNew,
      });
      toast.success(`Password updated · ${data.revoked_sessions || 0} other device(s) signed out`);
      setPwCurrent(""); setPwNew(""); setPwConfirm("");
      // Refresh sessions + history so the user sees the effect immediately
      try {
        const [s, h] = await Promise.all([
          API.get("/profile/sessions"),
          API.get("/profile/access-history"),
        ]);
        setSessions(s.data || []);
        setHistory(h.data || []);
      } catch { /* non-fatal */ }
    } catch (err) {
      toast.error(err?.response?.data?.detail || T.profileCouldntChangePw || "Couldn't change password");
    } finally {
      setSavingPw(false);
    }
  };

  const revokeSession = async (id) => {
    try {
      await API.delete(`/profile/sessions/${id}`);
      setSessions((prev) => prev.filter((s) => s.id !== id));
      toast.success("Session revoked");
    } catch (err) {
      toast.error(err?.response?.data?.detail || T.profileCouldntRevokeSession || "Couldn't revoke session");
    }
  };

  const revokeOthers = async () => {
    try {
      const { data } = await API.post("/profile/sessions/revoke-others");
      setSessions((prev) => prev.filter((s) => s.is_current));
      toast.success(`Signed out of ${data.count || 0} device(s)`);
    } catch (err) {
      toast.error(err?.response?.data?.detail || T.profileCouldntRevokeSessions || "Couldn't revoke sessions");
    }
  };

  // ── derived UI bits ──────────────────────────────────────────────────────
  const today      = productivity?.today || null;
  const weekTotal  = productivity?.week?.total_seconds ?? 0;
  const topApps    = productivity?.top_apps || [];
  const progressPct = Math.round(((today?.progress) ?? 0) * 100);

  const orgName = profile?.organization?.name || "Temveris";
  const roleLabel = useMemo(() => {
    const r = profile?.role || role || "employee";
    return r ? r.charAt(0).toUpperCase() + r.slice(1) : (T.profileRoleEmployee || "Employee");
  }, [profile, role]);

  // ─────────────────────────────────────────────────────────────────────────
  // RENDER
  // ─────────────────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div style={{
        minHeight: embedded ? 320 : "100vh", display: "flex",
        alignItems: "center", justifyContent: "center",
        background: embedded ? "transparent" : C.bg,
        color: C.brand, fontWeight: 600,
      }}>
        <div style={{
          width: 36, height: 36, borderRadius: "50%",
          border: `3px solid ${C.brandBg}`, borderTopColor: C.brand,
          animation: "zt-spin 0.8s linear infinite",
        }} />
        <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
      </div>
    );
  }

  // ── HERO STRIP (avatar + role + key stats) ───────────────────────────────
  // Clean card layout — no decorative gradient banner. Everything sits on
  // the surface colour and lets the content (name, badges, stats) lead.
  const hero = (
    <div className="zt-fade-up" style={{
      background: C.surface, border: `1px solid ${C.border}`, borderRadius: C.r,
      boxShadow: "0 1px 2px rgba(16,24,40,0.04)", marginBottom: 20, overflow: "hidden",
    }}>
      <div style={{
        padding: "24px 28px",
        display: "flex", alignItems: "center", gap: 22, flexWrap: "wrap",
      }}>
        {/* Avatar with a single soft brand-tinted ring (not a full conic
            halo — that overpowered the photo). Hover still lifts. */}
        <div style={{ position: "relative", flexShrink: 0, width: 88, height: 88 }}>
          <div style={{
            position: "relative", zIndex: 1,
            width: 88, height: 88, borderRadius: "50%",
            background: picture ? `url(${picture}) center/cover` : `linear-gradient(135deg,${C.brand},${C.brandDark})`,
            display: "flex", alignItems: "center", justifyContent: "center",
            color: "#fff", fontWeight: 800, fontSize: 28,
            border: `3px solid ${C.surface}`,
            boxShadow: `0 0 0 2px ${C.border}, 0 6px 16px rgba(16,24,40,0.10)`,
            transition: "box-shadow .2s ease",
            cursor: "default",
          }}
            onMouseEnter={(e) => {
              // Just shift the outer ring colour to brand on hover — no
              // scale, no extra lift. Crisp and professional.
              e.currentTarget.style.boxShadow = `0 0 0 2px ${C.brand}, 0 6px 16px rgba(16,24,40,0.10)`;
            }}
            onMouseLeave={(e) => {
              e.currentTarget.style.boxShadow = `0 0 0 2px ${C.border}, 0 6px 16px rgba(16,24,40,0.10)`;
            }}>
            {!picture && initials(name || profile?.name)}
          </div>
          {/* Live status dot — pulses while the worker is working so the
              hero reflects current activity at a glance. */}
          <span style={{
            position: "absolute", bottom: 2, right: 2, zIndex: 2,
            width: 16, height: 16, borderRadius: "50%",
            background: "#44B97B",
            border: `3px solid ${C.surface}`,
            animation: "zt-pulse-ring 2s ease-out infinite",
          }} />
        </div>
        <div style={{ flex: 1, minWidth: 240 }}>
          <h1 style={{ fontSize: 22, fontWeight: 800, color: C.text, margin: 0 }}>
            {name || profile?.name || "Unnamed user"}
          </h1>
          <p style={{ fontSize: 13, color: C.textSub, margin: "2px 0 0" }}>
            {profile?.email}
          </p>
          <div style={{ display: "flex", gap: 8, marginTop: 10, flexWrap: "wrap" }}>
            <span style={{
              padding: "3px 10px", fontSize: 11, fontWeight: 700,
              borderRadius: 999, background: C.brandBg, color: C.brand, letterSpacing: 0.4,
              textTransform: "uppercase",
            }}>{roleLabel}</span>
            {profile?.designation && (
              <span style={{
                padding: "3px 10px", fontSize: 11, fontWeight: 700,
                borderRadius: 999, background: C.borderLight, color: C.textSub, letterSpacing: 0.4,
              }}>{profile.designation}</span>
            )}
            {profile?.team && (
              <span style={{
                padding: "3px 10px", fontSize: 11, fontWeight: 700,
                borderRadius: 999, background: C.borderLight, color: C.textSub, letterSpacing: 0.4,
              }}>{profile.team}</span>
            )}
            <span style={{
              padding: "3px 10px", fontSize: 11, fontWeight: 700,
              borderRadius: 999, background: C.greenBg, color: C.green, letterSpacing: 0.4,
              textTransform: "uppercase",
            }}>{orgName}</span>
          </div>
        </div>
        {today && (
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            <Stat label="Today"      icon="clock"    value={fmtHM(today.working_seconds)}
                   sub={`${progressPct}% of ${Math.round((today.target_seconds || 0) / 3600)}h target`} />
            <Stat label="Active"     icon="activity" value={fmtHM(today.active_seconds)} accent={C.green} />
            <Stat label="Idle"       icon="pause"    value={fmtHM(today.idle_seconds)}   accent={C.amber} />
            <Stat label="This Week"  icon="bar"      value={fmtHM(weekTotal)}            accent={C.brand} />
          </div>
        )}
      </div>
      {/* Progress bar towards daily target — animated gradient fill so the
          worker can see the bar grow continuously through the day, and a
          subtle shimmer overlay indicates "active progression". */}
      {today?.target_seconds > 0 && (
        <div style={{ height: 8, background: C.borderLight, position: "relative", overflow: "hidden" }}>
          <div style={{
            width: `${progressPct}%`, height: "100%",
            background: progressPct >= 100
              ? `linear-gradient(90deg, ${C.green} 0%, #44B97B 100%)`
              : `linear-gradient(90deg, ${C.brand} 0%, #44B97B 100%)`,
            transition: "width .6s cubic-bezier(.22,1,.36,1)",
            position: "relative",
          }}>
            <div style={{
              position: "absolute", inset: 0,
              background: "linear-gradient(90deg, transparent 0%, rgba(255,255,255,.35) 50%, transparent 100%)",
              backgroundSize: "200% 100%",
              animation: "zt-progress-shimmer 2.4s linear infinite",
            }} />
          </div>
        </div>
      )}
    </div>
  );

  // ── TABS ─────────────────────────────────────────────────────────────────
  // Viewing someone else (or previewing the public view) → only the
  // Overview tab makes sense. The rest expose private workflows that
  // belong to the profile owner only.
  const tabBar = (
    <div style={{
      display: "flex", gap: 4, marginBottom: 20,
      borderBottom: `1px solid ${C.border}`, background: C.surface,
      borderRadius: `${C.r}px ${C.r}px 0 0`, padding: "0 12px",
      boxShadow: "0 1px 2px rgba(16,24,40,0.04)", overflowX: "auto",
    }}>
      <Tab label="Overview" active={activeTab === "overview"} onClick={() => setActiveTab("overview")} />
      {!readOnly && (<>
        <Tab label="Work"          active={activeTab === "work"}          onClick={() => setActiveTab("work")} />
        <Tab label="Security"      active={activeTab === "security"}      onClick={() => setActiveTab("security")}
             badge={sessions.length || null} />
        <Tab label="Notifications" active={activeTab === "notifications"} onClick={() => setActiveTab("notifications")} />
        <Tab label="Access"        active={activeTab === "history"}       onClick={() => setActiveTab("history")} />
      </>)}
    </div>
  );

  // ── TAB: OVERVIEW (personal info + productivity recap) ───────────────────
  const overviewPane = (
    <>
      <Card title={T.profilePersonalInfo || "Personal Information"} icon="user"
        action={readOnly
          ? <span style={{
              fontSize: 11, fontWeight: 700, letterSpacing: 0.5,
              padding: "5px 10px", borderRadius: 999,
              background: C.borderLight, color: C.textSub,
              textTransform: "uppercase",
            }}>{viewMode === "preview" ? (T.profilePreview || "Preview") : (T.profileReadOnly || "Read-only")}</span>
          : <Button onClick={saveProfile} disabled={savingProfile}>
              {savingProfile ? (T.profileSaving || "Saving…") : (T.profileSaveChanges || "Save Changes")}
            </Button>}>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
          <Field label="Full Name">
            <Input value={name} onChange={setName} placeholder="Your full name" autoComplete="name" disabled={readOnly} />
          </Field>
          <Field label={T.profileEmailAddress || "Email Address"} helper={readOnly ? null : (T.profileEmailHelper || "Email is your login identity and can't be changed here.")}>
            <Input value={profile?.email || ""} disabled />
          </Field>
          <Field label={T.profilePhoneNumber || "Phone Number"}>
            <Input
              value={phone}
              onChange={(v) => setPhone((v || "").replace(/[^\d+\-\s()]/g, ""))}
              placeholder="+1 555 123 4567"
              type="tel"
              autoComplete="tel"
              disabled={readOnly}
            />
          </Field>
          <Field label="Designation">
            <Input value={designation} onChange={setDesignation} placeholder="e.g. Senior Engineer" disabled={readOnly} />
          </Field>
          <Field label="Department">
            <Input value={department} onChange={setDepartment} placeholder="e.g. Engineering" disabled={readOnly} />
          </Field>
          <Field label={T.profileEmploymentType || "Employment Type"}>
            <Input value={(profile?.employment_type || "full_time").replace("_", " ")} disabled />
          </Field>
        </div>

        {/* Profile picture row is owner-only — visitors can already see
            the avatar in the hero and don't need the upload affordance. */}
        {!readOnly && (<>
          <div style={{ height: 1, background: C.borderLight, margin: "20px 0" }} />
          <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
            <div style={{
              width: 64, height: 64, borderRadius: "50%",
              background: picture ? `url(${picture}) center/cover` : `linear-gradient(135deg,${C.brand},${C.brandDark})`,
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "#fff", fontWeight: 800, fontSize: 20,
              border: `2px solid ${C.border}`, flexShrink: 0,
            }}>
              {!picture && initials(name)}
            </div>
            <div style={{ flex: 1 }}>
              <p style={{ fontSize: 13, fontWeight: 700, color: C.text, margin: 0 }}>{T.profilePicture_2 || "Profile picture"}</p>
              <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
                {T.profilePictureHint || "PNG, JPG, or WebP — automatically resized to 256×256."}
              </p>
            </div>
            <Button variant="secondary" onClick={handlePickFile}>Upload</Button>
            {picture && <Button variant="ghost" onClick={() => setPicture(null)}>Remove</Button>}
            <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp"
                   onChange={handleFileChange} style={{ display: "none" }} />
          </div>
        </>)}
      </Card>

      {/* Productivity Insights are owner-only — exposes screen-time / idle
          patterns that are not part of the public directory contract. */}
      {!readOnly && (
      <Card title={T.profileProductivityInsights || "Productivity Insights"} icon="trending">
        {!productivity ? (
          <p style={{ fontSize: 13, color: C.textSub, margin: 0 }}>{T.profileNoActivityToday || "No activity captured yet today."}</p>
        ) : (
          <>
            <div style={{ display: "flex", gap: 24, flexWrap: "wrap", marginBottom: 16 }}>
              <Stat label="Working Hours" value={fmtHM(today?.working_seconds || 0)} />
              <Stat label="Break Time"    value={fmtHM(today?.break_seconds || 0)} accent={C.amber} />
              <Stat label="Active Apps"   value={fmtHM(today?.active_seconds || 0)} accent={C.green} />
              <Stat label="Idle Time"     value={fmtHM(today?.idle_seconds || 0)}   accent={C.amber} />
            </div>
            <div>
              <p style={{ fontSize: 12, fontWeight: 700, color: C.text, letterSpacing: 0.4,
                          textTransform: "uppercase", margin: "0 0 8px" }}>
                Top Apps Today
              </p>
              {topApps.length === 0 ? (
                <p style={{ fontSize: 13, color: C.textSub, margin: 0 }}>
                  {T.profileNoAppActivity || "No app activity recorded — make sure the desktop tracker is running."}
                </p>
              ) : (
                <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
                  {topApps.map((app, i) => {
                    const max = Math.max(1, topApps[0].samples || 1);
                    const pct = Math.max(8, Math.round((app.samples / max) * 100));
                    return (
                      <li key={app.app_name + i} style={{ marginBottom: 8 }}>
                        <div style={{ display: "flex", justifyContent: "space-between",
                                      fontSize: 13, color: C.text, marginBottom: 4 }}>
                          <span style={{ fontWeight: 600, overflow: "hidden",
                                          textOverflow: "ellipsis", whiteSpace: "nowrap",
                                          maxWidth: 320 }}>
                            {app.app_name}
                          </span>
                          <span style={{ color: C.textSub }}>{app.samples} samples</span>
                        </div>
                        <div style={{ height: 6, background: C.borderLight, borderRadius: 3 }}>
                          <div style={{ width: `${pct}%`, height: "100%",
                                        background: C.brand, borderRadius: 3 }} />
                        </div>
                      </li>
                    );
                  })}
                </ul>
              )}
            </div>
          </>
        )}
      </Card>
      )}
    </>
  );

  // ── TAB: WORK PREFERENCES ───────────────────────────────────────────────
  const workPane = (
    <Card title="Work Preferences" icon="briefcase"
      action={<Button onClick={savePrefs} disabled={savingPrefs}>
        {savingPrefs ? "Saving…" : "Save Preferences"}
      </Button>}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
        <Field label={T.profileTimeZone || "Time Zone"}>
          <ReadOnly value={timezone} />
        </Field>
        <Field label={T.profileDailyTarget || "Daily Target (hours)"}>
          <ReadOnly value={targetHours == null ? "" : String(targetHours)} />
        </Field>
        <Field label={T.profileWorkingHoursStart || "Working Hours — Start"}>
          <ReadOnly value={workStart} />
        </Field>
        <Field label={T.profileWorkingHoursEnd || "Working Hours — End"}>
          <ReadOnly value={workEnd} />
        </Field>
      </div>

      <Field label={T.profileWorkingDays || "Working Days"}>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          {DAY_LABELS.map((label, idx) => {
            const on = workDays.includes(idx);
            return (
              <span key={label}
                style={{
                  minWidth: 56, padding: "8px 12px", borderRadius: C.rSm,
                  border: `1px solid ${on ? C.brand : C.border}`,
                  background: on ? C.brandBg : C.surface,
                  color: on ? C.brand : C.textLight,
                  fontWeight: on ? 700 : 500, fontSize: 13, textAlign: "center",
                }}>
                {label}
              </span>
            );
          })}
        </div>
      </Field>

      <p style={{ fontSize: 12, color: C.textSub, margin: "10px 0 0" }}>
        {T.profileScheduleReadOnly ||
          "Your shift, working days, daily target and time zone are set by your organization. Ask your administrator to change them."}
      </p>

      <div style={{ height: 1, background: C.borderLight, margin: "16px 0" }} />

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16 }}>
        <div>
          <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
            Available for scheduling
          </p>
          <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
            {T.profileAvailableHelper || "When off, managers and the scheduling module will treat you as unavailable."}
          </p>
        </div>
        <Toggle on={available} onChange={setAvailable} />
      </div>

      {!available && (
        <Field label="Away Message (optional)">
          <Input value={awayMessage} onChange={setAwayMessage}
                 placeholder="e.g. Out on annual leave until Friday" />
        </Field>
      )}
    </Card>
  );

  // ── TAB: SECURITY (password + sessions) ──────────────────────────────────
  const securityPane = (
    <>
      <Card title={T.profileChangePassword || "Change Password"} icon="shield">
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: 16 }}>
          <Field label={T.profileCurrentPassword || "Current Password"}>
            <Input type="password" value={pwCurrent} onChange={setPwCurrent}
                   autoComplete="current-password" />
          </Field>
          <div /> {/* spacer */}
          <Field label={T.profileNewPassword || "New Password"} helper={T.profileNewPasswordHelper || "At least 8 characters, with both letters and numbers."}>
            <Input type="password" value={pwNew} onChange={setPwNew}
                   autoComplete="new-password" />
          </Field>
          <Field label={T.profileConfirmNewPassword || "Confirm New Password"}>
            <Input type="password" value={pwConfirm} onChange={setPwConfirm}
                   autoComplete="new-password" />
          </Field>
        </div>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8 }}>
          <Button onClick={changePassword} disabled={savingPw || !pwCurrent || !pwNew || !pwConfirm}>
            {savingPw ? (T.profileUpdating || "Updating…") : (T.profileUpdatePassword || "Update Password")}
          </Button>
        </div>
        <p style={{ fontSize: 11, color: C.textLight, marginTop: 8 }}>
          {T.profilePasswordSignOutNote || "Updating your password signs you out of every other device automatically."}
        </p>
      </Card>

      <Card
        title={`Active Sessions (${sessions.length})`}
        icon="monitor"
        action={
          sessions.length > 1 ? (
            <Button variant="secondary" onClick={revokeOthers}>
              Sign out other devices
            </Button>
          ) : null
        }
        padding={0}
      >
        {sessions.length === 0 ? (
          <p style={{ padding: 24, fontSize: 13, color: C.textSub, margin: 0 }}>
            {T.profileNoActiveSessions || "No active sessions tracked yet. Sign in from a fresh device to populate this list."}
          </p>
        ) : (
          <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
            {sessions.map((s) => (
              <li key={s.id} style={{
                display: "flex", alignItems: "center", gap: 14,
                padding: "14px 24px", borderBottom: `1px solid ${C.borderLight}`,
              }}>
                <div style={{
                  width: 38, height: 38, borderRadius: 8,
                  background: s.is_current ? C.brandBg : C.borderLight,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  flexShrink: 0,
                }}>
                  <Icon n={s.device_type === "mobile" ? "smartphone" : "monitor"} s={18}
                        c={s.is_current ? C.brand : C.textSub} />
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0,
                              display: "flex", alignItems: "center", gap: 8 }}>
                    {s.device_name}
                    {s.is_current && (
                      <span style={{
                        fontSize: 10, padding: "1px 8px", borderRadius: 999,
                        background: C.greenBg, color: C.green, fontWeight: 800, letterSpacing: 0.4,
                      }}>{T.profileThisDevice || "THIS DEVICE"}</span>
                    )}
                  </p>
                  <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
                    {s.ip_address || "Unknown IP"}
                    {s.location && ` · ${s.location}`}
                    {" · "}Last active {fmtRel(s.last_active_at)}
                  </p>
                </div>
                {!s.is_current && (
                  <Button variant="ghost"
                          onClick={() => revokeSession(s.id)}
                          style={{ color: C.danger }}>
                    Revoke
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </>
  );

  // ── TAB: NOTIFICATIONS ──────────────────────────────────────────────────
  const notifRow = (key, title, sub) => (
    <div key={key} style={{
      display: "flex", alignItems: "center", justifyContent: "space-between",
      padding: "14px 0", borderBottom: `1px solid ${C.borderLight}`, gap: 16,
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>{title}</p>
        <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>{sub}</p>
      </div>
      <Toggle on={!!notif[key]} disabled={savingNotif}
              onChange={(v) => saveNotifications({ [key]: v })} />
    </div>
  );

  const notificationsPane = (
    <Card title={T.profileNotificationPreferences || "Notification Preferences"} icon="bell">
      <p style={{ fontSize: 13, color: C.textSub, margin: 0 }}>
        {T.profileNotifIntro || "Choose which alerts Temveris sends you. Changes save automatically."}
      </p>
      <div style={{ marginTop: 8 }}>
        {notifRow("schedule_email",   T.profileNotifScheduleEmail || "Schedule emails",       T.profileNotifScheduleEmailSub || "Email summary of your upcoming shifts and meetings.")}
        {notifRow("schedule_push",    T.profileNotifSchedulePush || "Schedule push alerts",  T.profileNotifSchedulePushSub || "Real-time push for shift starts, breaks, and reminders.")}
        {notifRow("activity_summary", T.profileNotifActivitySummary || "Daily activity summary",T.profileNotifActivitySummarySub || "End-of-day recap of working hours, idle time, and goals.")}
        {notifRow("leave_status",     T.profileNotifLeaveStatus || "Leave status updates",  T.profileNotifLeaveStatusSub || "Notifications when leave requests are approved, rejected, or commented on.")}
        {notifRow("system_updates",   T.profileNotifSystemUpdates || "System updates",        T.profileNotifSystemUpdatesSub || "Product release notes and platform announcements.")}
        {notifRow("security_alerts",  T.profileNotifSecurityAlerts || "Security alerts",       T.profileNotifSecurityAlertsSub || "New device sign-ins and password change confirmations.")}
        {notifRow("weekly_digest",    T.profileNotifWeeklyDigest || "Weekly digest",         T.profileNotifWeeklyDigestSub || "Friday recap email — productivity, hours, top focus areas.")}
      </div>
    </Card>
  );

  // ── TAB: ACCESS HISTORY ──────────────────────────────────────────────────
  const actionLabel = (a) => {
    switch (a) {
      case "UserLogin":       return "Sign-in";
      case "PasswordChanged": return "Password changed";
      case "SessionRevoked":  return "Session revoked";
      default:                return a;
    }
  };
  const actionAccent = (a) => {
    if (a === "PasswordChanged") return C.amber;
    if (a === "SessionRevoked")  return C.danger;
    return C.brand;
  };
  const historyPane = (
    <Card title={T.profileAccessHistory || "Access History"} icon="history" padding={0}>
      {history.length === 0 ? (
        <p style={{ padding: 24, fontSize: 13, color: C.textSub, margin: 0 }}>
          {T.profileNoAccessEvents || "No access events recorded yet."}
        </p>
      ) : (
        <ul style={{ listStyle: "none", padding: 0, margin: 0 }}>
          {history.map((row) => (
            <li key={row.id} style={{
              padding: "14px 24px", borderBottom: `1px solid ${C.borderLight}`,
              display: "flex", gap: 14, alignItems: "center",
            }}>
              <div style={{
                width: 8, height: 8, borderRadius: "50%",
                background: actionAccent(row.action), flexShrink: 0,
              }} />
              <div style={{ flex: 1, minWidth: 0 }}>
                <p style={{ fontSize: 14, fontWeight: 700, color: C.text, margin: 0 }}>
                  {actionLabel(row.action)}
                </p>
                <p style={{ fontSize: 12, color: C.textSub, margin: "2px 0 0" }}>
                  {row.ip_address || (T.profileUnknownIp || "Unknown IP")}
                  {row.metadata?.user_agent && ` · ${row.metadata.user_agent.slice(0, 80)}`}
                </p>
              </div>
              <p style={{ fontSize: 12, color: C.textLight, margin: 0, flexShrink: 0 }}>
                {fmtDateTime(row.at)}
              </p>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );

  // ── BODY (shared between standalone + embedded layouts) ──────────────────
  return (
    <CCtx.Provider value={C}>
    <ProfileFx />
    <div style={{
      maxWidth, margin: "0 auto",
      padding: embedded ? "8px 4px 32px" : "32px 24px 64px",
      fontFamily: "'DM Sans', system-ui, sans-serif",
    }}>
      {/* Viewing-mode banner — sits above the hero so the viewer always
          knows whether they're looking at their own profile, previewing
          their public view, or browsing a teammate. */}
      {viewMode !== "self" && (
        <div className="zt-fade-up" style={{
          display: "flex", alignItems: "center", gap: 12,
          padding: "10px 16px", marginBottom: 14,
          background: viewMode === "preview" ? C.amberBg : C.brandBg,
          border: `1px solid ${viewMode === "preview" ? "rgba(245,158,11,.35)" : C.brandBdr || C.brand}`,
          color: viewMode === "preview" ? C.amber : C.brand,
          borderRadius: C.r, fontSize: 13, fontWeight: 600,
        }}>
          <Icon n={viewMode === "preview" ? "info" : "user"} s={16}
                c={viewMode === "preview" ? C.amber : C.brand} />
          <span style={{ flex: 1 }}>
            {viewMode === "preview"
              ? "Preview — this is what teammates see. Save Changes is disabled until you exit preview."
              : `Viewing ${profile?.name || "teammate"} — read-only.`}
          </span>
          {viewMode === "preview" && (
            <Button variant="secondary" onClick={() => setPreviewPublic(false)}>{T.profileExitPreview || "Exit Preview"}</Button>
          )}
          {viewMode === "other" && onBack && (
            <Button variant="secondary" onClick={onBack}>{T.profileBack || "Back"}</Button>
          )}
        </div>
      )}

      {/* Owner-only Edit/Preview toggle — gives the user a way to see their
          own profile the way teammates see it without breaking edit flow. */}
      {viewMode === "self" && (
        <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
          <Button variant="secondary" onClick={() => setPreviewPublic(true)}>
            Preview public profile
          </Button>
        </div>
      )}

      {hero}
      {tabBar}
      {activeTab === "overview"      && overviewPane}
      {activeTab === "work"          && workPane}
      {activeTab === "security"      && securityPane}
      {activeTab === "notifications" && notificationsPane}
      {activeTab === "history"       && historyPane}
    </div>
    </CCtx.Provider>
  );
}

// ═════════════════════════════════════════════════════════════════════════════
// STANDALONE PAGE (default export, wired to /profile route)
// ═════════════════════════════════════════════════════════════════════════════
export default function Profile() {
  const navigate = useNavigate();
  const { T } = useLanguage();
  const { C } = useTheme();
  // /profile        → own profile (editable)
  // /profile/:userId → teammate's profile (read-only directory view)
  const { userId: routeUserId } = useParams();
  const numericUserId = routeUserId ? Number(routeUserId) : null;
  const handleBack = routeUserId ? () => navigate(-1) : null;

  const role = (localStorage.getItem("role") || "").toLowerCase();
  const isAdmin = role === "admin" || role === "hr";

  const body = (
    <main style={{ flex: 1, background: C.bg, overflowY: "auto", minWidth: 0 }}>
      <ProfileContent userId={numericUserId} onBack={handleBack} />
    </main>
  );

  if (isAdmin) {
    // Admin shell — matches AdminLeave / SettingsPage / Activity Log so the
    // sidebar + top bar (theme toggle, language, profile menu) is identical
    // across every admin-facing page. Without TopNavbar mounted here the
    // page jumped straight from the sidebar to the blue profile banner,
    // breaking visual continuity with the rest of the dashboard.
    return (
      <div style={{ display: "flex", height: "100vh", overflow: "hidden", background: C.bg,
                    fontFamily: "'DM Sans', system-ui, sans-serif" }}>
        <AdminSidebar activeKey="profile" />
        <main style={{ flex: 1, display: "flex", flexDirection: "column",
                       overflow: "hidden", minWidth: 0 }}>
          <TopNavbar
            title={T.myProfile || "My Profile"}
            subtitle={T.myProfileSubtitle || "Personal info, work preferences, security and access history"}
            onBack={() => navigate(-1)}
          />
          <div style={{ flex: 1, overflowY: "auto", background: C.bg }}>
            <ProfileContent />
          </div>
        </main>
      </div>
    );
  }

  // Employee shell — uses the shared TopNavbar so the profile dropdown,
  // theme toggle and language switcher are persistent across every page.
  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: C.bg,
                  fontFamily: "'DM Sans', system-ui, sans-serif" }}>
      <TopNavbar
        title={T.profile || T.myProfile || "My Profile"}
        subtitle={T.myProfileSubtitle || "Personal info and security"}
        onBack={() => navigate(-1)}
      />
      {body}
    </div>
  );
}
