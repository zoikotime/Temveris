import { useState, useEffect, useRef } from "react";
import { useLanguage } from "../context/LanguageContext";

/**
 * OfflineBanner
 * Sits at the top of the viewport. Slides in instantly when the browser goes
 * offline, shows a live "offline for m:ss" counter, then flashes a green
 * "reconnected" state and slides away ~2 s after the connection is restored.
 */

// Inject the keyframes once for the whole app (inline styles can't hold them).
const KEYFRAMES_ID = "zk-offline-banner-keyframes";
function ensureKeyframes() {
  if (typeof document === "undefined") return;
  if (document.getElementById(KEYFRAMES_ID)) return;
  const style = document.createElement("style");
  style.id = KEYFRAMES_ID;
  style.textContent = `
    @keyframes zkOfflineSlideDown { from { transform: translateY(-100%); opacity: 0; } to { transform: translateY(0); opacity: 1; } }
    @keyframes zkOfflineSlideUp   { from { transform: translateY(0); opacity: 1; } to { transform: translateY(-100%); opacity: 0; } }
    @keyframes zkOfflinePulse     { 0%,100% { transform: scale(1); opacity: 1; } 50% { transform: scale(1.6); opacity: .35; } }
    @keyframes zkOfflineEllipsis  { 0% { content: ""; } 25% { content: "."; } 50% { content: ".."; } 75%,100% { content: "..."; } }
    .zk-offline-dots::after { content: ""; animation: zkOfflineEllipsis 1.4s steps(1,end) infinite; }
    @media (prefers-reduced-motion: reduce) {
      .zk-offline-banner, .zk-offline-banner * { animation: none !important; transition: none !important; }
    }
  `;
  document.head.appendChild(style);
}

const fmtDuration = (ms) => {
  const total = Math.max(0, Math.floor(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
};

export default function OfflineBanner() {
  const { T } = useLanguage();
  const [online, setOnline]   = useState(navigator.onLine);
  const [showBack, setShowBack] = useState(false); // "back online" flash
  const [visible, setVisible]   = useState(false); // controls render
  const [leaving, setLeaving]   = useState(false); // slide-up before unmount
  const [offlineSince, setOfflineSince] = useState(null);
  const [now, setNow] = useState(() => Date.now());

  const timers = useRef([]);
  const clearTimers = () => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
  };

  useEffect(() => {
    ensureKeyframes();

    const goOffline = () => {
      clearTimers();
      setOnline(false);
      setShowBack(false);
      setLeaving(false);
      setOfflineSince(Date.now());
      setNow(Date.now());
      setVisible(true);
    };

    const goOnline = () => {
      clearTimers();
      setOnline(true);
      setShowBack(true);
      setLeaving(false);
      // Hold the green "reconnected" flash, then slide up and unmount.
      timers.current.push(setTimeout(() => setLeaving(true), 1800));
      timers.current.push(
        setTimeout(() => {
          setVisible(false);
          setShowBack(false);
          setLeaving(false);
          setOfflineSince(null);
        }, 2250),
      );
    };

    window.addEventListener("offline", goOffline);
    window.addEventListener("online", goOnline);
    return () => {
      window.removeEventListener("offline", goOffline);
      window.removeEventListener("online", goOnline);
      clearTimers();
    };
  }, []);

  // Tick the live "offline for" counter every second while disconnected.
  useEffect(() => {
    if (online || !visible || !offlineSince) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [online, visible, offlineSince]);

  if (!visible) return null;

  const isBack = online && showBack;
  const elapsed = offlineSince ? now - offlineSince : 0;

  const accent = isBack ? "#34D399" : "#FCA5A5";
  const gradient = isBack
    ? "linear-gradient(90deg, #1F7A4D 0%, #2F8F5C 50%, #1F7A4D 100%)"
    : "linear-gradient(90deg, #B91C1C 0%, #DC2626 50%, #B91C1C 100%)";

  return (
    <div
      className="zk-offline-banner"
      role="status"
      aria-live="polite"
      style={{
        position: "fixed", top: 0, left: 0, right: 0, zIndex: 99999,
        display: "flex", alignItems: "center", justifyContent: "center", gap: 12,
        padding: "10px 20px",
        background: gradient,
        color: "#fff",
        fontFamily: "'DM Sans','Inter',sans-serif",
        boxShadow: "0 6px 24px rgba(0,0,0,.28)",
        borderBottom: `1px solid ${isBack ? "rgba(255,255,255,.25)" : "rgba(255,255,255,.18)"}`,
        backdropFilter: "saturate(120%)",
        userSelect: "none",
        animation: `${leaving ? "zkOfflineSlideUp" : "zkOfflineSlideDown"} .45s cubic-bezier(.16,1,.3,1) both`,
      }}
    >
      {/* Status indicator: pulsing dot while offline, ring while online */}
      <span style={{ position: "relative", display: "inline-flex", width: 12, height: 12, flex: "0 0 auto" }}>
        {!isBack && (
          <span style={{
            position: "absolute", inset: 0, borderRadius: "50%", background: accent,
            animation: "zkOfflinePulse 1.6s ease-in-out infinite",
          }} />
        )}
        <span style={{
          position: "relative", margin: "auto", width: 8, height: 8,
          borderRadius: "50%", background: "#fff",
        }} />
      </span>

      {/* Icon */}
      {isBack ? (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#fff" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flex: "0 0 auto" }}>
          <path d="M5 12l5 5L20 7" />
        </svg>
      ) : (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" style={{ flex: "0 0 auto" }}>
          <path d="M1 1l22 22" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <path d="M10.71 5.05A16 16 0 0 1 22.56 9" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <path d="M8.53 16.11a6 6 0 0 1 6.95 0" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
          <circle cx="12" cy="20" r="1" fill="#fff" />
        </svg>
      )}

      {/* Two-line message */}
      <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.25, minWidth: 0 }}>
        <span style={{ fontSize: 13.5, fontWeight: 700, letterSpacing: ".1px" }}>
          {isBack ? (T.offlineBackOnline || "Back online") : (
            <span className="zk-offline-dots">{T.offlineNoConnection || "No internet connection · reconnecting"}</span>
          )}
        </span>
        <span style={{ fontSize: 11.5, fontWeight: 500, opacity: 0.92 }}>
          {isBack
            ? (T.offlineReconnected || "Reconnected successfully — resuming your session")
            : (T.offlineSessionSaved || "Your work session is saved · timer paused")}
        </span>
      </div>

      {/* Live "offline for" pill */}
      {!isBack && (
        <span style={{
          flex: "0 0 auto", marginLeft: 4,
          fontVariantNumeric: "tabular-nums",
          fontSize: 12, fontWeight: 700,
          padding: "3px 10px", borderRadius: 999,
          background: "rgba(0,0,0,.18)",
          border: "1px solid rgba(255,255,255,.22)",
        }}>
          {(T.offlineFor || "Offline for {time}").replace("{time}", fmtDuration(elapsed))}
        </span>
      )}
    </div>
  );
}
