/**
 * Bot-challenge widget for the public signup form (Cloudflare Turnstile).
 *
 * Public organization creation mints a tenant, an admin account and a trial
 * subscription from an unauthenticated request, so it needs a proof-of-humanity
 * gate. This component only PRODUCES a token; the server verifies it against the
 * provider before any database write (see app/commercial/challenge.py). A
 * rendered widget proves nothing on its own.
 *
 * CONFIGURATION
 *   VITE_TURNSTILE_SITE_KEY   public site key — see ./challengeConfig.js
 *
 * When the key is absent the component renders nothing, which mirrors how the
 * backend degrades without SIGNUP_CHALLENGE_SECRET: local dev and CI keep
 * working, and the pair is enabled together in production.
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { TURNSTILE_SITE_KEY as SITE_KEY } from "./challengeConfig";

const SCRIPT_ID = "cf-turnstile-script";
const SCRIPT_SRC =
  "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";

/** Load the provider script once per page, shared across mounts. */
let scriptPromise = null;
function loadTurnstile() {
  if (window.turnstile) return Promise.resolve(window.turnstile);
  if (scriptPromise) return scriptPromise;

  scriptPromise = new Promise((resolve, reject) => {
    const existing = document.getElementById(SCRIPT_ID);
    if (existing) {
      existing.addEventListener("load", () => resolve(window.turnstile));
      existing.addEventListener("error", () => reject(new Error("script failed")));
      return;
    }
    const el = document.createElement("script");
    el.id = SCRIPT_ID;
    el.src = SCRIPT_SRC;
    el.async = true;
    el.defer = true;
    el.onload = () => resolve(window.turnstile);
    el.onerror = () => reject(new Error("script failed"));
    document.head.appendChild(el);
  });
  return scriptPromise;
}

/**
 * @param {(token: string|null) => void} onToken
 *        Called with the token on success, and with null whenever the token
 *        stops being valid (expiry, error, reset) so the parent can re-disable
 *        submission rather than posting a stale token.
 */
export default function SignupChallenge({ onToken }) {
  const holder = useRef(null);
  const widgetId = useRef(null);
  const [status, setStatus] = useState(SITE_KEY ? "loading" : "disabled");

  // Keep the latest callback in a ref so re-renders of the parent never cause
  // the widget to be torn down and re-created (which would clear a solved
  // challenge and look like a random reset to the user).
  const cb = useRef(onToken);
  useEffect(() => { cb.current = onToken; }, [onToken]);

  const reset = useCallback(() => {
    cb.current?.(null);
    if (widgetId.current && window.turnstile) {
      try { window.turnstile.reset(widgetId.current); } catch { /* already gone */ }
    }
  }, []);

  useEffect(() => {
    if (!SITE_KEY) return;
    let cancelled = false;

    loadTurnstile()
      .then((turnstile) => {
        if (cancelled || !holder.current || !turnstile) return;
        widgetId.current = turnstile.render(holder.current, {
          sitekey: SITE_KEY,
          callback: (token) => { setStatus("solved"); cb.current?.(token); },
          "error-callback": () => { setStatus("error"); cb.current?.(null); },
          "expired-callback": () => { setStatus("expired"); cb.current?.(null); },
          "timeout-callback": () => { setStatus("expired"); cb.current?.(null); },
        });
        setStatus("ready");
      })
      .catch(() => {
        if (!cancelled) setStatus("error");
      });

    return () => {
      cancelled = true;
      if (widgetId.current && window.turnstile) {
        try { window.turnstile.remove(widgetId.current); } catch { /* already gone */ }
      }
      widgetId.current = null;
    };
  }, []);

  if (status === "disabled") return null;

  return (
    <div style={{ margin: "14px 0" }}>
      <div ref={holder} data-testid="signup-challenge" />
      {status === "loading" && (
        <p style={hint} aria-live="polite">Loading verification…</p>
      )}
      {status === "expired" && (
        <p style={{ ...hint, color: "#92400E" }} aria-live="polite">
          Verification expired.{" "}
          <button type="button" onClick={reset} style={linkBtn}>Verify again</button>
        </p>
      )}
      {status === "error" && (
        <p style={{ ...hint, color: "#991B1B" }} role="alert">
          We couldn't load the verification check. Disable any ad blocker and{" "}
          <button type="button" onClick={reset} style={linkBtn}>try again</button>.
        </p>
      )}
    </div>
  );
}

const hint = { fontSize: 12, color: "#64748B", marginTop: 6 };
const linkBtn = {
  background: "none", border: "none", padding: 0, font: "inherit",
  color: "#11496A", textDecoration: "underline", cursor: "pointer",
};
