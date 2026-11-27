/**
 * Supabase client singleton.
 *
 * Used ONLY for OAuth sign-in (Google, etc.). The rest of the app continues
 * to authenticate against the Temveris backend with a custom JWT.
 *
 * Required env vars (in frontend/.env or frontend/.env.local):
 *
 *   VITE_SUPABASE_URL=https://<project-ref>.supabase.co
 *   VITE_SUPABASE_ANON_KEY=<anon public key from Supabase dashboard>
 *
 * `isSupabaseConfigured()` lets the UI gracefully degrade when the operator
 * hasn't wired these — the "Continue with Google" button falls back to the
 * existing backend-orchestrated SSO flow.
 */
import { createClient } from "@supabase/supabase-js";

const SUPABASE_URL  = import.meta.env.VITE_SUPABASE_URL || "";
const SUPABASE_ANON = import.meta.env.VITE_SUPABASE_ANON_KEY || "";

export const supabase = (SUPABASE_URL && SUPABASE_ANON)
  ? createClient(SUPABASE_URL, SUPABASE_ANON, {
      auth: {
        persistSession:    true,
        autoRefreshToken:  true,
        detectSessionInUrl: true,
        flowType:          "pkce",
        storageKey:        "zt_supabase_auth",
      },
    })
  : null;

export const isSupabaseConfigured = () => !!supabase;

/**
 * Build the absolute redirect URL Supabase should send the user to after
 * Google confirms consent. Must be one of the "Redirect URLs" listed in the
 * Supabase project's Auth → URL Configuration.
 *
 * In Electron `window.location.origin` is `file://` which Supabase won't
 * accept — fall back to the configured production origin in that case.
 */
export const supabaseRedirectUrl = () => {
  const path = "/sso/callback?provider=supabase-google";
  if (typeof window === "undefined") return path;
  const origin = window.location.origin;
  if (!origin || origin.startsWith("file:")) {
    // Same default as PUBLIC_ORIGIN in config.js (not imported: config.js reads
    // window at module load, and this helper must stay safe without a window).
    const fallback = import.meta.env.VITE_PUBLIC_ORIGIN || "https://app.temveris.example";
    return `${fallback.replace(/\/+$/, "")}${path}`;
  }
  return `${origin}${path}`;
};
