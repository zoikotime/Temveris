/**
 * Bot-challenge configuration.
 *
 * Split out of SignupChallenge.jsx so that file exports only a component —
 * mixing a component and a helper in one module breaks Fast Refresh, which the
 * project's ESLint config enforces (react-refresh/only-export-components).
 *
 * When no site key is configured the widget renders nothing and the signup form
 * does not wait for a token, mirroring how the backend degrades without
 * SIGNUP_CHALLENGE_SECRET. The pair is enabled together in production; see
 * app/commercial/challenge.py.
 */

export const TURNSTILE_SITE_KEY = (
  import.meta.env.VITE_TURNSTILE_SITE_KEY || ""
).trim();

/**
 * Whether a challenge is configured for this deployment.
 *
 * Lets the signup form distinguish "no challenge required here" from "challenge
 * not solved yet" — without it, an unconfigured deployment would disable its own
 * submit button forever waiting for a token no widget will ever produce.
 */
export function isChallengeConfigured() {
  return Boolean(TURNSTILE_SITE_KEY);
}
