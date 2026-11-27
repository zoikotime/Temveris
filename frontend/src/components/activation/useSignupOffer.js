/**
 * The trial offer shown on the activation flow's Account step.
 *
 * Every number on the plan-summary panel — the per-worker price, the annual
 * discount, the trial length, the trial end date, and how much is due today —
 * is served by `GET /api/v1/billing/quote/preview`, which prices from the same
 * `quotes.compute_amounts` the paid checkout path charges from. Nothing here
 * computes money or dates: a frontend that derived "$8/worker" or "today + 14"
 * on its own would quietly disagree with the invoice the moment either was
 * changed server-side.
 *
 * The panel is informational, so a failed fetch degrades the panel and never
 * blocks signup — the form beside it still works.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE } from "../../config";

/**
 * Legal document versions to fall back on when the offer request fails.
 *
 * The consent record is stamped with whatever version the page agreed to, so
 * something has to be sent. These mirror `constants.TERMS_VERSION_CURRENT` /
 * `PRIVACY_VERSION_CURRENT`; the served values are preferred whenever the
 * request succeeds, which is what keeps the two from drifting in practice.
 */
export const FALLBACK_TERMS_VERSION = "2026-05-01";
export const FALLBACK_PRIVACY_VERSION = "2026-05-01";

/**
 * @param {string} plan  plan code to price (the server validates it against the
 *                       self-serve allow-list and 400s on anything else).
 */
export function useSignupOffer(plan) {
  const [offer, setOffer] = useState(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  // Guards a late response from a superseded request overwriting a newer one.
  const requestId = useRef(0);

  useEffect(() => {
    const id = ++requestId.current;
    let cancelled = false;

    setLoading(true);
    (async () => {
      try {
        const res = await fetch(
          `${API_BASE}/api/v1/billing/quote/preview` +
            `?plan=${encodeURIComponent(plan)}&billing_cycle=annual`,
          { headers: { Accept: "application/json" } },
        );
        if (cancelled || id !== requestId.current) return;
        if (!res.ok) {
          setOffer(null);
          setError(true);
          return;
        }
        setOffer(await res.json());
        setError(false);
      } catch {
        if (cancelled || id !== requestId.current) return;
        setOffer(null);
        setError(true);
      } finally {
        if (!cancelled && id === requestId.current) setLoading(false);
      }
    })();

    return () => { cancelled = true; };
  }, [plan, reloadKey]);

  const retry = useCallback(() => setReloadKey((k) => k + 1), []);

  return { offer, error, loading, retry };
}
