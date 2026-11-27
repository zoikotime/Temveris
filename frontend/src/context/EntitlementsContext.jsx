/**
 * EntitlementsContext — Phase 3.7 (presentation only).
 *
 * Fetches the backend actor projection (GET /api/v1/entitlements/projection)
 * and exposes it as an indexed { capabilityKey: presentation } map plus a
 * `presentationFor(cap)` helper. The backend is authoritative; this context
 * neither computes nor caches entitlement *decisions* — it stores presentation
 * hints for rendering.
 *
 * Actor/workspace isolation (spec §11): the projection is refetched whenever the
 * signed-in user id changes, and the previous actor's projection is cleared
 * immediately on that change so one actor's presentation can never be shown to
 * another. Nothing is written to localStorage — presentation is per-actor and
 * lives only in memory for the current session.
 *
 * This is NOT an authorization boundary. It only drives SHOW/LOCK/HIDE/
 * READ_ONLY presentation. The server remains the sole authority on access.
 */
import React, {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
} from "react";

import API from "../api/axios";
import { useAuth } from "./AuthContext";
import { indexProjection, PRESENTATION } from "../entitlements/registry";

const EntitlementsContext = createContext({
  projection: null,
  loading: false,
  error: null,
  ready: false,
  presentationFor: () => null,
  refresh: () => {},
});

export function EntitlementsProvider({ children }) {
  const { user } = useAuth();
  const userId = user?.id ?? null;

  const [projection, setProjection] = useState(null);  // { cap: presentation } | null
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // Guards against a stale response from a previous actor overwriting a newer
  // one (§11): every fetch stamps the actor it was issued for.
  const activeActor = useRef(null);

  const load = useCallback(async (forActor) => {
    setLoading(true);
    setError(null);
    try {
      const { data } = await API.get("/api/v1/entitlements/projection");
      // Only accept the response if the actor has not changed underneath us.
      if (activeActor.current === forActor) {
        setProjection(indexProjection(data));
      }
    } catch (e) {
      if (activeActor.current === forActor) {
        // Safe fallback (§12): keep projection null. Consumers treat "no
        // projection" as pre-3.7 behaviour — render normally, decorate nothing.
        // This never fabricates premium access because the backend does not
        // enforce entitlements; a shown item cannot bypass a server check.
        setProjection(null);
        setError(e);
      }
    } finally {
      if (activeActor.current === forActor) setLoading(false);
    }
  }, []);

  useEffect(() => {
    // Clear the previous actor's view synchronously before fetching the new
    // one, so a workspace/user switch never flashes stale entitlements.
    activeActor.current = userId;
    setProjection(null);
    setError(null);
    if (userId == null) {
      setLoading(false);
      return;
    }
    load(userId);
  }, [userId, load]);

  const presentationFor = useCallback(
    (cap) => (projection ? projection[cap] || null : null),
    [projection],
  );

  const value = useMemo(() => ({
    projection,
    loading,
    error,
    ready: projection !== null,
    presentationFor,
    refresh: () => load(activeActor.current),
  }), [projection, loading, error, presentationFor, load]);

  return (
    <EntitlementsContext.Provider value={value}>
      {children}
    </EntitlementsContext.Provider>
  );
}

export function useEntitlements() {
  return useContext(EntitlementsContext);
}

export { PRESENTATION };
