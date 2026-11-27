import React from "react";
// TEMPORARY: blink/reload investigation. Remove with the rest of the
// instrumentation once the root cause is confirmed.
import { blinkLog } from "../lib/blinkLog";

// Detect errors that are caused by a failed network fetch of a JS chunk.
// These look like "Failed to fetch dynamically imported module" or "ChunkLoadError".
function isNetworkChunkError(error) {
  if (!error) return false;
  const msg = (error.message || error.name || "").toLowerCase();
  return (
    msg.includes("failed to fetch") ||
    msg.includes("dynamically imported module") ||
    msg.includes("chunkloaderror") ||
    msg.includes("loading chunk") ||
    msg.includes("loading css chunk") ||
    msg.includes("networkerror")
  );
}

// Sentinel that survives a single page reload. We use it to auto-recover
// from "stale cached index.html → 404 on a chunk hash that no longer exists"
// after a fresh deploy: first failure → reload once (which re-fetches the
// new index.html with current chunk hashes); second failure → render the
// version-mismatch UI so the user can reset manually.
const RELOAD_SENTINEL = "zt_chunk_reload_attempted";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = {
      hasError: false,
      isChunk:  false,        // chunk fetch failed (likely stale cache)
      error:    null,
      online:   navigator.onLine,
    };
    this._onOnline  = () => this.setState({ online: true });
    this._onOffline = () => this.setState({ online: false });
  }

  componentDidMount() {
    window.addEventListener("online",  this._onOnline);
    window.addEventListener("offline", this._onOffline);
    // Successful mount means the new bundle loaded fine; clear any stale
    // sentinel so the next chunk error gets its one auto-reload chance.
    try { sessionStorage.removeItem(RELOAD_SENTINEL); } catch {}
  }

  componentWillUnmount() {
    window.removeEventListener("online",  this._onOnline);
    window.removeEventListener("offline", this._onOffline);
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, isChunk: isNetworkChunkError(error), error };
  }

  componentDidCatch(error, info) {
    console.error("ErrorBoundary caught:", error, info.componentStack);

    // [BlinkInvestigation] record every ErrorBoundary activation with enough
    // context to classify it as A (ChunkLoadError) vs C (generic React crash).
    const chunk = isNetworkChunkError(error);
    blinkLog("errorboundary-caught", {
      isChunk: chunk,
      online: navigator.onLine,
      message: (error?.message || error?.name || "").toString().slice(0, 400),
      componentStack: info?.componentStack
        ? String(info.componentStack).slice(0, 800)
        : undefined,
    });

    // Stale-cache auto-recovery: when the app is online but a chunk fetch
    // 404'd, the cached index.html is pointing at filenames from a previous
    // deploy. Force one full reload — index.html is re-fetched from the
    // server with the current chunk hashes and the app loads cleanly.
    // Guarded by a sessionStorage sentinel so we never loop if the reload
    // itself also fails.
    if (isNetworkChunkError(error) && navigator.onLine) {
      try {
        const already = sessionStorage.getItem(RELOAD_SENTINEL) === "1";
        if (!already) {
          sessionStorage.setItem(RELOAD_SENTINEL, "1");
          blinkLog("errorboundary-auto-reload", { reason: "stale-chunk" });
          window.location.reload();
        } else {
          blinkLog("errorboundary-auto-reload-suppressed", {
            reason: "sentinel-already-set (showing stale-cache UI)",
          });
        }
      } catch {
        // sessionStorage unavailable — skip the sentinel; render the UI.
      }
    }
  }

  handleRetry = () => {
    // The "reload" the user clicks should always start fresh — clear the
    // sentinel so a follow-up chunk error gets a new auto-reload attempt.
    try { sessionStorage.removeItem(RELOAD_SENTINEL); } catch {}
    blinkLog("errorboundary-manual-reload", {
      isChunk: this.state.isChunk,
      online: this.state.online,
    });
    this.setState({ hasError: false, isChunk: false, error: null });
    window.location.reload();
  };

  render() {
    if (!this.state.hasError) return this.props.children;

    const offline      = !this.state.online;
    const stalecache   = this.state.isChunk && this.state.online;

    // ── True offline ───────────────────────────────────────────────────────
    if (offline) {
      return (
        <div style={{
          position: "fixed", inset: 0, display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center",
          background: "linear-gradient(135deg,#F0F9FF 0%,#E0F2FE 100%)",
          fontFamily: "'DM Sans','Inter',sans-serif", gap: 0,
        }}>
          <div style={{ marginBottom: 28 }}>
            <svg width="90" height="90" viewBox="0 0 24 24" fill="none">
              <path d="M1 1l22 22" stroke="#CBD5E1" strokeWidth="2" strokeLinecap="round"/>
              <path d="M16.72 11.06A10.94 10.94 0 0 1 19 12.55" stroke="#94A3B8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M5 12.55a10.94 10.94 0 0 1 5.17-2.39" stroke="#94A3B8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M10.71 5.05A16 16 0 0 1 22.56 9" stroke="#CBD5E1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M1.42 9a15.91 15.91 0 0 1 4.7-2.88" stroke="#CBD5E1" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M8.53 16.11a6 6 0 0 1 6.95 0" stroke="#94A3B8" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
              <circle cx="12" cy="20" r="1" fill="#64748B"/>
            </svg>
          </div>
          <h2 style={{ fontSize: 22, fontWeight: 800, color: "#0F172A", margin: "0 0 10px" }}>
            No Internet Connection
          </h2>
          <p style={{ fontSize: 14, color: "#64748B", margin: "0 0 6px", maxWidth: 320, textAlign: "center", lineHeight: 1.6 }}>
            Please check your Wi-Fi or network cable and try again.
          </p>
          <p style={{ fontSize: 13, color: "#94A3B8", margin: "0 0 28px", maxWidth: 320, textAlign: "center" }}>
            Your work session is safe — it will resume automatically once you reconnect.
          </p>
          <button
            onClick={this.handleRetry}
            style={{
              padding: "11px 28px", background: "#0EA5E9", color: "#fff",
              border: "none", borderRadius: 10, fontWeight: 700, fontSize: 14,
              cursor: "pointer", fontFamily: "inherit", letterSpacing: ".2px",
              boxShadow: "0 4px 14px rgba(14,165,233,.35)",
            }}
            onMouseEnter={e => e.currentTarget.style.background = "#0284C7"}
            onMouseLeave={e => e.currentTarget.style.background = "#0EA5E9"}
          >
            Try Again
          </button>
        </div>
      );
    }

    // ── Stale cache: app is online, but a JS chunk 404'd (likely after a
    //    deploy). The auto-reload in componentDidCatch already tried once
    //    and failed, so prompt the user to do a manual hard-refresh /
    //    reinstall — there's nothing the runtime can do at this point.
    if (stalecache) {
      return (
        <div style={{
          position: "fixed", inset: 0, display: "flex", flexDirection: "column",
          alignItems: "center", justifyContent: "center",
          background: "linear-gradient(135deg,#F0FDF4 0%,#DCFCE7 100%)",
          fontFamily: "'DM Sans','Inter',sans-serif", padding: 24,
        }}>
          <div style={{ marginBottom: 22 }}>
            <svg width="80" height="80" viewBox="0 0 24 24" fill="none">
              <path d="M21 12a9 9 0 11-3.5-7.13" stroke="#16A34A" strokeWidth="2" strokeLinecap="round"/>
              <path d="M21 4v6h-6" stroke="#16A34A" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </div>
          <h2 style={{ fontSize: 22, fontWeight: 800, color: "#0F172A", margin: "0 0 10px" }}>
            App was updated
          </h2>
          <p style={{ fontSize: 14, color: "#475569", margin: "0 0 6px", maxWidth: 380, textAlign: "center", lineHeight: 1.6 }}>
            A newer version of Temveris is available. Reload to load the latest build — your work session is safe.
          </p>
          <p style={{ fontSize: 12, color: "#94A3B8", margin: "10px 0 28px", maxWidth: 380, textAlign: "center", lineHeight: 1.6 }}>
            If reload doesn't fix it: press <strong>Ctrl+Shift+R</strong>, or quit and re-open the app.
          </p>
          <button
            onClick={this.handleRetry}
            style={{
              padding: "11px 28px",
              background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)",
              color: "#fff", border: "none", borderRadius: 10,
              fontWeight: 700, fontSize: 14, cursor: "pointer",
              fontFamily: "inherit", letterSpacing: ".2px",
              boxShadow: "0 4px 14px rgba(68,185,123,.45)",
            }}
          >
            Reload Now
          </button>
        </div>
      );
    }

    // ── Generic non-network error ──────────────────────────────────────────
    return (
      <div style={{ padding: 40, textAlign: "center", fontFamily: "'DM Sans','Inter',sans-serif" }}>
        <h2 style={{ color: "#B42318", marginBottom: 12 }}>Something went wrong</h2>
        <p style={{ color: "#667085", marginBottom: 20, fontSize: 14 }}>
          {this.state.error?.message || "An unexpected error occurred."}
        </p>
        <button
          onClick={this.handleRetry}
          style={{ padding: "10px 24px", background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)", color: "#fff", border: "none", borderRadius: 8, fontWeight: 700, fontSize: 14, cursor: "pointer", boxShadow: "0 4px 14px rgba(17,73,106,0.30)" }}
        >
          Reload Page
        </button>
      </div>
    );
  }
}
