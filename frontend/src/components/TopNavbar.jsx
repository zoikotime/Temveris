/**
 * Temveris — Reusable Top Navbar
 * ===============================
 * The standard application header shared by every "inside-the-app" page
 * (dashboard, leave, settings, account-settings, etc).
 *
 * Renders, from left → right:
 *   • optional page title  /  optional left slot (back button, breadcrumbs…)
 *   • spacer
 *   • optional right slot for page-specific actions (Save / Refresh…)
 *   • dark/light mode toggle  (driven by ThemeContext)
 *   • LangSwitcher
 *   • profile menu  →  Account Settings / Sign Out
 *
 * Drop into any page like:
 *
 *   <TopNavbar title="Track Settings"
 *              right={<SaveButton />}
 *              onBack={() => navigate(-1)} />
 */
import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import LangSwitcher from "./LangSwitcher";
import { useTheme } from "../context/ThemeContext";
import { useLanguage } from "../context/LanguageContext";
import { useAuth } from "../context/AuthContext";
import { useWorkspace } from "../context/WorkspaceContext";
import TemverisIcon from "../assets/Temveris_Icon_PNG.png";

const initials = (name) =>
  (name || "").split(" ").map(w => w[0]).join("").toUpperCase().slice(0, 2) || "U";

// Inline icons — we deliberately do NOT pull lucide-react into the leaf
// pages to keep the chunk size flat.
const Icon = ({ d, size = 16, color = "#475467", strokeWidth = 2 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
    stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round"
    style={{ display: "inline-block", flexShrink: 0 }}>
    {d.split(" M").map((seg, i) => (
      <path key={i} d={i === 0 ? seg : "M" + seg} />
    ))}
  </svg>
);

const SUN  = "M12 18a6 6 0 100-12 6 6 0 000 12z M12 2v4 M12 18v4 M4.22 4.22l2.83 2.83 M16.95 16.95l2.83 2.83 M2 12h4 M18 12h4 M4.22 19.78l2.83-2.83 M16.95 7.05l2.83-2.83";
const MOON = "M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z";
const BACK = "M19 12H5 M12 19l-7-7 7-7";
const CHEV = "M6 9l6 6 6-6";

export default function TopNavbar({
  title       = "",
  subtitle    = "",
  onBack      = null,
  left        = null,    // optional custom left slot — overrides title
  right       = null,    // page-specific actions
  showProfile = true,
}) {
  const { isDark, toggleDark, C } = useTheme();
  const { T } = useLanguage();
  const { user, logout } = useAuth();
  const { workspaceName, workspaceLogo } = useWorkspace();
  const navigate = useNavigate();
  const [profileOpen, setProfileOpen] = useState(false);
  const profileRef = useRef(null);

  // Close profile menu on outside click
  useEffect(() => {
    const onClick = (e) => {
      if (profileRef.current && !profileRef.current.contains(e.target)) {
        setProfileOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  // Prefer the live user from AuthContext; fall back to localStorage during
  // the brief window before /auth/me resolves on first paint.
  const adminUser = (() => {
    if (user) {
      return {
        name: user.name || "User",
        email: user.email || "",
        profile_picture: user.profile_picture || null,
      };
    }
    try {
      const name  = localStorage.getItem("name")  || localStorage.getItem("user_name")  || "User";
      const email = localStorage.getItem("email") || localStorage.getItem("user_email") || "";
      const profile_picture = localStorage.getItem("profile_picture") || null;
      return { name, email, profile_picture };
    } catch { return { name: "User", email: "", profile_picture: null }; }
  })();

  const handleSignOut = () => {
    setProfileOpen(false);
    logout({ redirect: false });
    navigate("/", { replace: true });
  };

  return (
    <header
      style={{
        height: 56, flexShrink: 0,
        background: C.surface,
        borderBottom: `1px solid ${C.border}`,
        display: "flex", alignItems: "center", gap: 10,
        padding: "0 20px",
        position: "sticky", top: 0, zIndex: 100,
      }}
    >
      {/* ── Left: back button + title (or fully custom slot) ── */}
      {left ? left : (
        <div style={{ display: "flex", alignItems: "center", gap: 12, minWidth: 0 }}>
          {onBack && (
            <button
              type="button"
              onClick={onBack}
              title={T.back || "Back"}
              style={{
                width: 34, height: 34, borderRadius: C.rSm,
                border: `1px solid ${C.border}`, background: C.surface,
                display: "flex", alignItems: "center", justifyContent: "center",
                cursor: "pointer", flexShrink: 0,
              }}
            >
              <Icon d={BACK} size={15} color={C.textSub} />
            </button>
          )}
          <div style={{ minWidth: 0 }}>
            {title && (
              <p style={{
                fontSize: 16, fontWeight: 700, color: C.text,
                lineHeight: 1.2, margin: 0,
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {title}
              </p>
            )}
            {subtitle && (
              <p style={{
                fontSize: 11, color: C.textSub, margin: "2px 0 0",
                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
              }}>
                {subtitle}
              </p>
            )}
          </div>
        </div>
      )}

      {/* spacer */}
      <div style={{ flex: 1 }} />

      {/* page-specific right slot */}
      {right}

      {/* ── Dark/Light toggle ── */}
      <button
        type="button"
        onClick={toggleDark}
        title={isDark ? (T.switchToLightMode || "Switch to light mode") : (T.switchToDarkMode || "Switch to dark mode")}
        style={{
          width: 34, height: 34, borderRadius: "50%",
          border: `1px solid ${C.border}`, background: C.surface,
          display: "flex", alignItems: "center", justifyContent: "center",
          cursor: "pointer", flexShrink: 0,
        }}
      >
        <Icon d={isDark ? SUN : MOON} size={16} color={C.textSub} />
      </button>

      {/* ── Language switcher ── */}
      <LangSwitcher isDark={isDark} />

      {/* ── Profile menu ── */}
      {showProfile && (
        <div ref={profileRef} style={{ position: "relative" }}>
          <button
            type="button"
            onClick={() => setProfileOpen(o => !o)}
            aria-label={T.openProfileMenu || "Open profile menu"}
            style={{
              width: 34, height: 34, borderRadius: "50%",
              background: adminUser.profile_picture
                ? `url(${adminUser.profile_picture}) center/cover`
                : `linear-gradient(135deg,${C.brand} 0%,${C.brandAccent || "#44B97B"} 100%)`,
              display: "flex", alignItems: "center", justifyContent: "center",
              color: "#fff", fontWeight: 800, fontSize: 13, cursor: "pointer",
              boxShadow: profileOpen ? `0 0 0 3px ${C.brandBg}` : "0 2px 8px rgba(17,73,106,0.4)",
              border: profileOpen ? `1.5px solid ${C.brand}` : "none",
              padding: 0, outline: "none",
              transition: "box-shadow .15s, border .15s",
            }}
          >
            {!adminUser.profile_picture && initials(adminUser.name)}
          </button>

          {profileOpen && (
            <div style={{
              position: "absolute", top: "calc(100% + 10px)", right: 0,
              width: 240, background: C.surface, borderRadius: 10,
              border: `1px solid ${C.border}`,
              boxShadow: isDark
                ? "0 12px 32px rgba(0,0,0,0.5), 0 2px 8px rgba(0,0,0,0.4)"
                : "0 12px 32px rgba(16,24,40,0.14), 0 2px 8px rgba(16,24,40,0.06)",
              padding: "6px 0", zIndex: 1000,
              fontFamily: "inherit",
            }}>
              <div style={{ padding: "10px 16px 8px" }}>
                <p style={{ fontSize: 10, fontWeight: 700, color: C.textLight, letterSpacing: 0.6, textTransform: "uppercase", marginBottom: 6 }}>
                  {T.organisation || "Organisation"}
                </p>
                <button
                  type="button"
                  title={T.goToDashboard || "Go to dashboard"}
                  onClick={() => {
                    setProfileOpen(false);
                    navigate(["admin", "hr"].includes(user?.role) ? "/admin" : "/dashboard");
                  }}
                  style={{
                    width: "100%", display: "flex", alignItems: "center", gap: 10,
                    padding: "8px 10px", borderRadius: 6,
                    border: `1px solid ${C.border}`, background: C.bg,
                    cursor: "pointer", fontFamily: "inherit", textAlign: "left",
                    transition: "background 0.15s, border-color 0.15s",
                  }}
                  onMouseEnter={(e) => { e.currentTarget.style.background = C.brandAccentBg; e.currentTarget.style.borderColor = C.brandAccent; }}
                  onMouseLeave={(e) => { e.currentTarget.style.background = C.bg; e.currentTarget.style.borderColor = C.border; }}
                >
                  {workspaceLogo ? (
                    <img src={workspaceLogo} alt="" style={{ width: 32, height: 32, borderRadius: 6, objectFit: "cover", flexShrink: 0, border: `1px solid ${C.border}` }} />
                  ) : (
                    // No org-uploaded logo → show the official Temveris brand
                    // icon instead of a bare initial (per HR bug #4).
                    <div style={{ width: 32, height: 32, borderRadius: 6, background: C.brandBg, display: "flex", alignItems: "center", justifyContent: "center", flexShrink: 0, padding: 3, boxSizing: "border-box" }}>
                      <img src={TemverisIcon} alt="Temveris" style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: "block" }} />
                    </div>
                  )}
                  <span style={{ fontSize: 13, fontWeight: 600, color: C.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", flex: 1 }}>
                    {workspaceName || adminUser.name}
                  </span>
                </button>
                {adminUser.email && (
                  <p style={{ fontSize: 11, color: C.textSub, margin: "6px 0 0", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    {adminUser.email}
                  </p>
                )}
              </div>

              <div style={{ height: 1, background: C.borderLight, margin: "6px 0" }} />

              <button
                type="button"
                onClick={() => { setProfileOpen(false); navigate("/profile"); }}
                style={{
                  width: "100%", textAlign: "left", padding: "10px 16px",
                  border: "none", background: "transparent",
                  color: C.brand, fontWeight: 700, fontSize: 14,
                  cursor: "pointer", fontFamily: "inherit",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = C.brandAccentBg; e.currentTarget.style.color = C.brandAccent; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = C.brand; }}
              >
                {T.myProfile || "My Profile"}
              </button>
              <button
                type="button"
                onClick={() => { setProfileOpen(false); navigate("/account-settings"); }}
                style={{
                  width: "100%", textAlign: "left", padding: "10px 16px",
                  border: "none", background: "transparent",
                  color: C.text, fontWeight: 600, fontSize: 14,
                  cursor: "pointer", fontFamily: "inherit",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = C.brandAccentBg; e.currentTarget.style.color = C.brandAccent; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = C.text; }}
              >
                {T.accountSettings || "Account Settings"}
              </button>

              <button
                type="button"
                onClick={handleSignOut}
                style={{
                  width: "100%", textAlign: "left", padding: "10px 16px",
                  border: "none", background: "transparent",
                  color: C.text, fontWeight: 600, fontSize: 14,
                  cursor: "pointer", fontFamily: "inherit",
                }}
                onMouseEnter={(e) => { e.currentTarget.style.background = C.brandAccentBg; e.currentTarget.style.color = C.brandAccent; }}
                onMouseLeave={(e) => { e.currentTarget.style.background = "transparent"; e.currentTarget.style.color = C.text; }}
              >
                {T.signOut || "Sign Out"}
              </button>
            </div>
          )}
        </div>
      )}
    </header>
  );
}
