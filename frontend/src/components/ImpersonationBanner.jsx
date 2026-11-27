/**
 * ImpersonationBanner — fixed top bar shown whenever a super_admin is
 * impersonating a tenant user.
 *
 * Detection: presence of `zt_sa_return_token` in localStorage.
 * That key is written by SuperAdmin.jsx before it switches the active token.
 *
 * Return flow:
 *   1. Restore the SA token via AuthContext.login() (re-fetches /auth/me).
 *   2. Clear all impersonation markers from localStorage.
 *   3. Navigate to /super-admin — no full page reload needed.
 *
 * Layout: renders two sibling nodes in one React fragment —
 *   • A fixed-positioned visual bar (z-index 9999, top: 0).
 *   • A same-height spacer div in normal flow so page content is never
 *     obscured by the fixed bar, regardless of which page is shown.
 */
import { useCallback, useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAuth } from "../context/AuthContext";

const BANNER_H = 46; // px — keep in sync with the inline height below

// Routes where the banner must never appear.
const HIDDEN_PREFIXES = [
  "/super-admin",
  "/",             // login
  "/forgot-password",
  "/verify-otp",
  "/reset-password",
  "/set-password",
  "/sso/callback",
  "/start",
  "/verify-email",
  "/invite",
  "/contact-sales",
];

function shouldHide(pathname) {
  if (pathname === "/") return true;
  return HIDDEN_PREFIXES.slice(1).some((p) => pathname.startsWith(p));
}

export default function ImpersonationBanner() {
  const { user, login } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const [returning, setReturning] = useState(false);

  const [saToken, setSaToken] = useState(
    () => localStorage.getItem("zt_sa_return_token") || null,
  );

  // Re-check after every route change (impersonation starts with a full reload,
  // but the return happens in-place, so we also need the effect below).
  useEffect(() => {
    setSaToken(localStorage.getItem("zt_sa_return_token") || null);
  }, [pathname]);

  // Cross-tab: if the SA signs out in another tab the key disappears.
  useEffect(() => {
    const onStorage = () =>
      setSaToken(localStorage.getItem("zt_sa_return_token") || null);
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const handleReturn = useCallback(async () => {
    if (!saToken || returning) return;
    setReturning(true);
    try {
      // Wipe impersonation markers before switching tokens so the banner
      // disappears immediately and never flickers on the SA portal.
      localStorage.removeItem("zt_sa_return_token");
      localStorage.removeItem("zt_sa_return_role");
      setSaToken(null);

      // Restore SA session — login() writes the token, then fetches /auth/me.
      await login(saToken, null, { remember: true });
      navigate("/super-admin", { replace: true });
    } catch {
      // If something goes wrong, hard-redirect so we don't leave the user stuck.
      window.location.assign("/super-admin");
    } finally {
      setReturning(false);
    }
  }, [saToken, returning, login, navigate]);

  if (!saToken || shouldHide(pathname)) return null;

  const displayName = user?.name || user?.email || "a user";
  const displayEmail = user?.name && user?.email ? user.email : null;
  const displayRole = user?.role || "";

  return (
    <>
      {/* Fixed visual bar */}
      <div
        role="alert"
        aria-live="polite"
        style={{
          position: "fixed",
          top: 0, left: 0, right: 0,
          height: BANNER_H,
          zIndex: 9999,
          background: "linear-gradient(90deg,#7F1D1D 0%,#B91C1C 60%,#991B1B 100%)",
          color: "#fff",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 20px",
          gap: 12,
          fontFamily: "'DM Sans', system-ui, sans-serif",
          fontSize: 13,
          boxShadow: "0 2px 10px rgba(127,29,29,0.45)",
          userSelect: "none",
        }}
      >
        {/* Left: identity */}
        <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
          {/* Pill */}
          <span style={{
            flexShrink: 0,
            background: "rgba(255,255,255,0.18)",
            border: "1px solid rgba(255,255,255,0.3)",
            borderRadius: 4,
            padding: "2px 8px",
            fontSize: 10,
            fontWeight: 800,
            letterSpacing: 0.6,
            textTransform: "uppercase",
          }}>
            Impersonating
          </span>

          {/* Name */}
          <span style={{ fontWeight: 700, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 260 }}>
            {displayName}
          </span>

          {/* Email (if different from display name) */}
          {displayEmail && (
            <span style={{ opacity: 0.7, fontSize: 12, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis", maxWidth: 220 }}>
              {displayEmail}
            </span>
          )}

          {/* Role badge */}
          {displayRole && (
            <span style={{
              flexShrink: 0,
              background: "rgba(255,255,255,0.15)",
              borderRadius: 4,
              padding: "2px 7px",
              fontSize: 11,
              fontWeight: 600,
              textTransform: "capitalize",
            }}>
              {displayRole}
            </span>
          )}
        </div>

        {/* Right: return button */}
        <button
          onClick={handleReturn}
          disabled={returning}
          style={{
            flexShrink: 0,
            background: "#fff",
            color: "#B91C1C",
            border: "none",
            borderRadius: 6,
            padding: "7px 18px",
            fontWeight: 700,
            fontSize: 13,
            cursor: returning ? "not-allowed" : "pointer",
            fontFamily: "inherit",
            opacity: returning ? 0.75 : 1,
            transition: "opacity 0.15s",
            whiteSpace: "nowrap",
          }}
        >
          {returning ? "Returning…" : "← Return to Super Admin"}
        </button>
      </div>

      {/* Flow spacer — same height as the fixed bar so sticky headers sit below it */}
      <div style={{ height: BANNER_H, flexShrink: 0 }} aria-hidden="true" />
    </>
  );
}
