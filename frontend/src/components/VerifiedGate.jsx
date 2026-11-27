import React, { useEffect, useState } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { readToken } from "../api/authStorage";
import { API_BASE } from "../config";

// Authenticated paths an unverified self-serve signup must STILL be able to
// reach: the verify screen itself, and the checkout flow a "subscribe" signup
// enters before it verifies (Step 1 hands it a session for exactly that
// walk-through). Prefix-matched, so nested paths (/subscription/configure,
// /workspace/ready, …) are covered. Every OTHER guarded route is withheld until
// the email is confirmed.
export const VERIFY_EXEMPT_PREFIXES = [
  "/verify-email", "/verify-otp", "/subscription", "/setup", "/onboarding", "/workspace",
];

/**
 * Withholds the product from a self-serve signup that has not verified its email.
 *
 * Signup issues a session at Step 1 — BEFORE the OTP step — so the visitor can
 * be walked through activation. The side effect is that the browser Back button
 * from the verify screen would otherwise land on a fully working dashboard,
 * making email verification skippable. This gate closes that hole: an unverified
 * signup is returned to /verify-email until the code is entered.
 *
 * Scoped so it can NEVER lock out anyone already working — it gates ONLY the one
 * case that is unambiguously a fresh self-serve signup, and fails OPEN on every
 * other outcome:
 *   - /activation/status/me reports a commercial signup row AND
 *     email_verified === false  → gated (a self-serve signup mid-activation);
 *   - an existing / super-admin-provisioned tenant has no signup row → allowed;
 *   - a caller with no commercial org at all gets 404 → allowed;
 *   - any network or parse error, or the still-in-flight check → allowed.
 *
 * Like SetupGate this is a navigation convenience, not a security boundary — the
 * backend remains the authority on every request regardless of where the UI goes.
 */
export default function VerifiedGate({ children }) {
  const { pathname } = useLocation();
  const exempt = VERIFY_EXEMPT_PREFIXES.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
  // `null` = undecided (request in flight). Resolved straight to `false` in the
  // initializer whenever there is nothing to ask — no token, or an exempt path —
  // so those never fetch and never redirect.
  const [mustVerify, setMustVerify] = useState(
    () => (readToken() && !exempt ? null : false),
  );

  useEffect(() => {
    if (exempt) { setMustVerify(false); return undefined; }
    const token = readToken();
    if (!token) { setMustVerify(false); return undefined; }

    let cancelled = false;
    fetch(`${API_BASE}/api/v1/activation/status/me`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        // Gate ONLY a fresh self-serve signup that has not verified yet.
        if (!cancelled) {
          setMustVerify(Boolean(d && d.signup_id && d.email_verified === false));
        }
      })
      .catch(() => { if (!cancelled) setMustVerify(false); });
    return () => { cancelled = true; };
  }, [pathname, exempt]);

  // Undecided falls through to `children` (fail-open), matching SetupGate: a
  // blank hold would put every verified admin behind a network round-trip, and
  // the redirect still fires the instant the server answers.
  if (mustVerify) return <Navigate to="/verify-email" replace />;
  return children;
}
