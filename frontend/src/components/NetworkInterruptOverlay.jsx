import { useState, useEffect } from "react";
import { useConnection } from "../context/ConnectionContext";
import { useLanguage } from "../context/LanguageContext";
import { API_BASE } from "../config";
// TEMPORARY: blink/reload investigation. Remove with the rest of the
// instrumentation once the root cause is confirmed.
import { blinkLog } from "../lib/blinkLog";

const getToken = () =>
  localStorage.getItem("zt_token") || localStorage.getItem("token") || null;

/**
 * NetworkInterruptOverlay
 * Full-screen blocker shown whenever either the browser is offline OR the
 * backend health probe fails. While it is visible the ClockInWidget freezes
 * its timer (via ConnectionContext), and the Refresh button below reconciles
 * state with the server and resumes the previous clock-in time.
 */
export default function NetworkInterruptOverlay() {
  const { connected, offlineSince, pingHealth, clearOffline } = useConnection();
  const { T } = useLanguage();
  const [refreshing, setRefreshing] = useState(false);

  // [BlinkInvestigation] record when this full-screen blocker shows/hides —
  // candidate B (overlay covers the UI → "buttons stop working").
  useEffect(() => {
    blinkLog("network-overlay", {
      visible: !connected,
      browserOnline: navigator.onLine,
      offlineSince: offlineSince || null,
    });
  }, [connected, offlineSince]);

  if (connected) return null;

  const handleRefresh = async () => {
    setRefreshing(true);
    await pingHealth();

    // If we're back online, tell the backend to exclude the offline window
    // from work time so the resumed timer picks up where it paused.
    if (navigator.onLine && offlineSince) {
      const tok = getToken();
      if (tok) {
        try {
          await fetch(`${API_BASE}/employees/network-pause`, {
            method: "POST",
            headers: {
              Authorization: `Bearer ${tok}`,
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              offline_start: new Date(offlineSince).toISOString(),
              offline_end:   new Date().toISOString(),
            }),
          });
        } catch {
          // If the reconcile call fails we still let the user back in;
          // the widget's own session restore will refetch from the server.
        }
      }
      clearOffline();
      blinkLog("network-overlay-reload", { offlineSince: offlineSince || null });
      // Reload to re-run session restore with fresh server state.
      window.location.reload();
      return;
    }

    setRefreshing(false);
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="zt-netint-title"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 100000,
        background: "linear-gradient(135deg,#E7F0F6 0%,#E8F7EE 100%)",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        padding: 24,
        fontFamily: "'DM Sans','Inter','Segoe UI',sans-serif",
      }}
    >
      {/* WiFi with red alert badge — matches the reference icon */}
      <div style={{ position: "relative", width: 180, height: 110, marginBottom: 28 }}>
        <svg
          width="180"
          height="110"
          viewBox="0 0 180 110"
          fill="none"
          xmlns="http://www.w3.org/2000/svg"
        >
          <path d="M30 55 Q90 5 150 55" stroke="#5EEAD4" strokeWidth="10" strokeLinecap="round" fill="none"/>
          <path d="M50 72 Q90 40 130 72" stroke="#2DD4BF" strokeWidth="10" strokeLinecap="round" fill="none"/>
          <path d="M70 88 Q90 72 110 88" stroke="#14B8A6" strokeWidth="10" strokeLinecap="round" fill="none"/>
          <circle cx="90" cy="102" r="6" fill="#0F766E"/>
        </svg>
        <div
          style={{
            position: "absolute",
            top: 4,
            right: 6,
            width: 46,
            height: 46,
            borderRadius: "50%",
            background: "#EF4444",
            boxShadow: "0 6px 16px rgba(239,68,68,.45)",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            color: "#fff",
            fontWeight: 900,
            fontSize: 28,
            lineHeight: 1,
            fontFamily: "'DM Sans','Inter',sans-serif",
          }}
        >
          !
        </div>
      </div>

      <h2
        id="zt-netint-title"
        style={{ fontSize: 22, fontWeight: 800, color: "#0F172A", margin: "0 0 10px", textAlign: "center" }}
      >
        {T.connectionInterrupted || "Connection Interrupted"}
      </h2>
      <p style={{ fontSize: 14, color: "#64748B", margin: "0 0 6px", maxWidth: 360, textAlign: "center", lineHeight: 1.6 }}>
        {T.connectionInterruptedBody || "We lost contact with the server. Your clock-in timer is paused so this period won't be counted as work time."}
      </p>
      <p style={{ fontSize: 13, color: "#94A3B8", margin: "0 0 28px", maxWidth: 360, textAlign: "center" }}>
        {T.connectionInterruptedHint || "Your session is safe. Tap Refresh once your network is back to resume from where you left off."}
      </p>

      <button
        onClick={handleRefresh}
        disabled={refreshing}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 10,
          padding: "12px 32px",
          background: refreshing ? "#A6C7DC" : "linear-gradient(135deg,#11496A 0%,#44B97B 100%)",
          color: "#fff",
          border: "none",
          borderRadius: 10,
          fontWeight: 700,
          fontSize: 14,
          cursor: refreshing ? "wait" : "pointer",
          fontFamily: "inherit",
          letterSpacing: ".2px",
          boxShadow: "0 4px 14px rgba(17,73,106,.35)",
          transition: "background .15s",
        }}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="#fff"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          style={{
            animation: refreshing ? "zt-spin 0.9s linear infinite" : "none",
          }}
        >
          <path d="M23 4v6h-6" />
          <path d="M1 20v-6h6" />
          <path d="M3.51 9a9 9 0 0114.85-3.36L23 10" />
          <path d="M20.49 15A9 9 0 015.64 18.36L1 14" />
        </svg>
        {refreshing ? (T.refreshing || "Refreshing...") : (T.refresh || "Refresh")}
      </button>

      <style>{`@keyframes zt-spin{to{transform:rotate(360deg)}}`}</style>
    </div>
  );
}
