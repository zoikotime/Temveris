// Public unsubscribe landing page (no auth required).
//
// Backs the List-Unsubscribe link. Reads `email` + `token` from the query
// string and confirms the opt-out against the backend
// (POST /notifications/unsubscribe). Per the Commercial Email Communications
// Estate, unsubscribe applies to marketing / subscribed messages only —
// essential account, security, attendance, approval, and billing
// communications are always delivered.
import { useEffect, useState } from "react";
import { API_BASE } from "../config";

const BRAND = "#009BD6";

function useQuery() {
  const params = new URLSearchParams(window.location.search);
  return { email: params.get("email") || "", token: params.get("token") || "" };
}

export default function Unsubscribe() {
  const { email, token } = useQuery();
  const [state, setState] = useState("working"); // working | done | error | resubscribed

  async function call(path) {
    const url =
      `${API_BASE}${path}?email=${encodeURIComponent(email)}` +
      `&token=${encodeURIComponent(token)}`;
    const res = await fetch(url, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    return data && data.ok;
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      if (!email || !token) {
        if (!cancelled) setState("error");
        return;
      }
      try {
        const ok = await call("/notifications/unsubscribe");
        if (!cancelled) setState(ok ? "done" : "error");
      } catch {
        if (!cancelled) setState("error");
      }
    })();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleResubscribe() {
    try {
      const ok = await call("/notifications/resubscribe");
      setState(ok ? "resubscribed" : "error");
    } catch {
      setState("error");
    }
  }

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#F1F5F9",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        fontFamily: "'Segoe UI', Helvetica, Arial, sans-serif",
        padding: "16px",
      }}
    >
      <div
        style={{
          maxWidth: 520,
          width: "100%",
          background: "#fff",
          borderRadius: 16,
          padding: 40,
          textAlign: "center",
          boxShadow: "0 1px 3px rgba(0,0,0,0.08)",
        }}
      >
        <div style={{ fontSize: 24, fontWeight: 700, marginBottom: 20 }}>
          <span style={{ color: BRAND }}>Temveris</span>
        </div>

        {state === "working" && (
          <p style={{ color: "#475569", fontSize: 15 }}>Processing your request…</p>
        )}

        {state === "done" && (
          <>
            <h1 style={{ color: "#0F172A", fontSize: 22, margin: "0 0 12px" }}>
              You&rsquo;re unsubscribed
            </h1>
            <p style={{ color: "#475569", fontSize: 15, lineHeight: 1.6, margin: 0 }}>
              <strong>{email}</strong> will no longer receive Temveris marketing email.
            </p>
            <button
              onClick={handleResubscribe}
              style={{
                marginTop: 24,
                background: BRAND,
                color: "#fff",
                border: 0,
                borderRadius: 8,
                padding: "12px 24px",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
              }}
            >
              Re-subscribe
            </button>
          </>
        )}

        {state === "resubscribed" && (
          <>
            <h1 style={{ color: "#0F172A", fontSize: 22, margin: "0 0 12px" }}>
              You&rsquo;re re-subscribed
            </h1>
            <p style={{ color: "#475569", fontSize: 15, lineHeight: 1.6, margin: 0 }}>
              <strong>{email}</strong> will receive Temveris marketing email again.
            </p>
          </>
        )}

        {state === "error" && (
          <>
            <h1 style={{ color: "#DC2626", fontSize: 22, margin: "0 0 12px" }}>
              Link not valid
            </h1>
            <p style={{ color: "#475569", fontSize: 15, lineHeight: 1.6, margin: 0 }}>
              This unsubscribe link is invalid or has expired. Please use the link from a
              recent Temveris email, or contact support@temveris.example.
            </p>
          </>
        )}

        <p style={{ color: "#94A3B8", fontSize: 13, marginTop: 24, lineHeight: 1.6 }}>
          This affects Temveris marketing email only. Essential account, security,
          attendance, approval, and billing communications are always delivered.
        </p>
      </div>
    </div>
  );
}
