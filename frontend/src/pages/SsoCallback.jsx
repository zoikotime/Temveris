import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import AuthLayout from "../components/auth/AuthLayout";
import { useAuth } from "../context/AuthContext";
import { supabase, isSupabaseConfigured } from "../lib/supabase";
import API from "../api/axios";

/**
 * Landing page the backend redirects to after the user completes Google
 * sign-in.  Two shapes:
 *
 *   /sso/callback?token=<jwt>&provider=google&remember=1&role=admin&name=...&email=...
 *   /sso/callback?error=<code>&provider=google&email=<addr>
 *
 * On success we persist the token via AuthContext.login() (which picks
 * localStorage or sessionStorage based on the `remember` flag) and route the
 * user to /admin or /dashboard.  On error we render a friendly card with a
 * "back to sign in" link — never a blank page, never silent failure.
 */
const ERROR_COPY = {
  "no-account": (email) =>
    email
      ? `${email} is not registered in this workspace. Ask your administrator to invite you, then try again.`
      : "This account isn't registered in your workspace. Ask your administrator to invite you, then try again.",
  "account-disabled": () =>
    "This account has been disabled. Contact your administrator to restore access.",
  "email-not-verified": () =>
    "Your provider hasn't confirmed this email address. Verify it first, then retry sign-in.",
  "not-configured": (_, provider) =>
    `${capitalize(provider)} sign-in isn't configured for this workspace yet. Use email + password, or contact your administrator.`,
  "bad-state": () =>
    "This sign-in link expired or was tampered with. Please try again.",
  "missing-code": () =>
    "Sign-in was cancelled before it finished.",
  "token-exchange-failed": () =>
    "Couldn't reach the identity provider. Check your internet connection and retry.",
  "token-rejected": () =>
    "The identity provider rejected the sign-in. Try again in a moment.",
  "userinfo-failed": () =>
    "Couldn't read your account profile from the identity provider. Try again.",
  "userinfo-rejected": () =>
    "The identity provider refused to share your profile. Try again.",
  "no-access-token": () =>
    "The identity provider returned an unexpected response. Try again.",
  "no-email": () =>
    "Your account didn't share an email address with us. Use a different sign-in method.",
};

function capitalize(s) {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "";
}

function resolveErrorMessage(code, email, provider) {
  const fn = ERROR_COPY[code];
  if (fn) return fn(email, provider);
  return `Sign-in failed${code ? ` (${code})` : ""}. Please try again.`;
}

export default function SsoCallback() {
  const navigate = useNavigate();
  const { login } = useAuth();
  const [errorMsg, setErrorMsg] = useState(null);
  // StrictMode in dev fires effects twice — guard so we don't double-toast.
  const handled = useRef(false);

  useEffect(() => {
    if (handled.current) return;
    handled.current = true;

    const params   = new URLSearchParams(window.location.search);
    const token    = params.get("token");
    const error    = params.get("error");
    const provider = params.get("provider") || "sso";
    const email    = params.get("email") || "";

    // ── Path A: Supabase OAuth callback ────────────────────────────────
    // Supabase puts its access_token in either the query string (PKCE flow
    // after `?code=` is exchanged) or the URL hash (implicit flow). The JS
    // client handles both — we just listen for the resulting session and
    // hand the access token to our backend bridge for a Temveris JWT.
    const isSupabaseCallback =
      provider === "supabase-google" ||
      params.get("code") ||
      window.location.hash.includes("access_token=");

    if (isSupabaseCallback && isSupabaseConfigured()) {
      const remember = params.get("remember") !== "0";
      (async () => {
        try {
          // Give the Supabase client a tick to ingest the URL.
          let session = (await supabase.auth.getSession())?.data?.session;
          if (!session) {
            // Wait briefly for detectSessionInUrl to finish.
            await new Promise((r) => setTimeout(r, 350));
            session = (await supabase.auth.getSession())?.data?.session;
          }
          if (!session?.access_token) {
            throw new Error("Supabase did not return a session.");
          }

          const res = await API.post("/auth/sso/supabase-exchange", {
            access_token: session.access_token,
            remember,
          });
          const data = res?.data || {};
          if (!data.access_token) {
            throw new Error("Backend did not issue a token.");
          }
          const user = data.user || {};
          await login(
            data.access_token,
            {
              id:              user.id,
              name:            user.name,
              email:           user.email,
              role:            user.role,
              profile_picture: user.profile_picture || null,
              organization_id: user.organization_id ?? null,
            },
            { remember },
          );
          // We've minted our own JWT — drop the Supabase session so future
          // visits don't try to re-bridge an expired Supabase token.
          await supabase.auth.signOut().catch(() => {});

          toast.success("Signed in with Google");
          window.history.replaceState({}, "", "/sso/callback");
          const role = (user.role || "").toLowerCase();
          if (window.isDesktopApp === true) {
            navigate("/electron", { replace: true });
          } else if (role === "super_admin") {
            navigate("/super-admin", { replace: true });
          } else if (["admin", "hr"].includes(role)) {
            navigate("/admin", { replace: true });
          } else {
            navigate("/dashboard", { replace: true });
          }
        } catch (e) {
          const detail = e?.response?.data?.detail;
          const code   = (detail && (detail.code || detail)) || "token-rejected";
          const msg    = resolveErrorMessage(typeof code === "string" ? code : "token-rejected", detail?.email || "", "google");
          setErrorMsg(msg);
          window.history.replaceState({}, "", "/sso/callback");
        }
      })();
      return;
    }

    // ── Path B: legacy backend-redirect SSO callback ───────────────────
    if (error || !token) {
      const msg = resolveErrorMessage(error || "missing-code", email, provider);
      setErrorMsg(msg);
      // Scrub the URL so a reload doesn't repeat the error banner.
      window.history.replaceState({}, "", "/sso/callback");
      return;
    }

    const remember = params.get("remember") !== "0";
    const role     = (params.get("role") || "").toLowerCase();
    const name     = params.get("name") || "";
    const emailQ   = params.get("email") || "";

    (async () => {
      try {
        await login(
          token,
          // Pass a minimal user object so AuthContext can mirror role/name
          // immediately — the next /auth/me refresh fills in the rest.
          name || role || emailQ
            ? { name, email: emailQ, role: role || "employee" }
            : null,
          { remember },
        );
        toast.success(`Signed in with ${capitalize(provider)}`);

        // Bounce by role. Mirror the post-login routing in Login.jsx so
        // password and SSO sign-ins land identically.
        if (window.isDesktopApp === true) {
          navigate("/electron", { replace: true });
        } else if (role === "super_admin") {
          navigate("/super-admin", { replace: true });
        } else if (["admin", "hr"].includes(role)) {
          navigate("/admin", { replace: true });
        } else {
          navigate("/dashboard", { replace: true });
        }
      } catch {
        setErrorMsg("Couldn't finish sign-in. Please try again.");
      }
    })();
    // We intentionally run this once on mount — `login` is stable via useCallback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (errorMsg) {
    return (
      <AuthLayout
        footer={
          <a
            href="/"
            className="font-semibold text-brand-blue hover:underline underline-offset-2"
          >
            ← Back to sign in
          </a>
        }
      >
        <div className="text-center">
          <h1 className="text-[22px] font-bold tracking-tight text-slate-900">
            Sign-in didn't complete
          </h1>
          <p className="mt-3 text-[14px] leading-relaxed text-slate-600">
            {errorMsg}
          </p>
        </div>
      </AuthLayout>
    );
  }

  return (
    <AuthLayout>
      <div className="text-center py-4">
        <div className="mx-auto mb-4 h-9 w-9 rounded-full border-[3px] border-slate-200 border-t-brand-blue animate-spin" />
        <h1 className="text-[18px] font-semibold text-slate-800">
          Completing sign-in…
        </h1>
        <p className="mt-1.5 text-[13px] text-slate-500">
          Securing your session.
        </p>
      </div>
    </AuthLayout>
  );
}
