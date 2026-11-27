/**
 * Phase 3.25 — the single reusable in-page entitlement gate.
 *
 * <Gate capability="..."> and useCapability("...") are the ONLY way in-page
 * features consult entitlements. They consume the existing backend projection
 * (via EntitlementsContext.presentationFor) and render the existing five
 * presentation states. There is deliberately NO plan logic, no role logic and
 * no capability list here — the backend `/api/v1/entitlements/projection`
 * already accounts for plan, tier, lifecycle, jurisdiction, grandfathering and
 * the actor's role. This file only turns a presentation string into a render
 * decision, exactly like registry.js does for the sidebar.
 *
 * Presentation → behaviour (mirrors the backend vocabulary):
 *   SHOW                   → render the feature normally.
 *   LOCK                   → render the upgrade/locked presentation. LOCK vs
 *                            HIDE is the backend's decision (is_upsellable +
 *                            role): a sellable tiered capability the actor lacks
 *                            comes back as LOCK; a privacy-denied or unbuilt
 *                            capability comes back as HIDE. The Gate never makes
 *                            that choice itself.
 *   HIDE                   → render nothing (privacy / permanently unbuilt).
 *   READ_ONLY              → render the content; the caller disables the
 *                            restricted mutation using the `readOnly` flag.
 *   CONFIGURATION_REQUIRED → render a setup CTA, never an upsell.
 *
 * Safe fallback (§12): when the projection has not resolved (null — still
 * loading, or the fetch failed) presentationFor returns null and the Gate
 * treats it as SHOW. The frontend never invents a restriction; only an explicit
 * backend presentation restricts. This matches registry.shouldRenderNav and is
 * safe because the backend does not (yet) enforce entitlements — a shown item
 * can never grant access the server would otherwise deny.
 *
 * This is NOT an authorization boundary. It drives visibility only. The server
 * remains the sole authority on access (backend 403 enforcement is a separate,
 * still-OFF phase).
 */
import React from "react";

import { useEntitlements } from "../context/EntitlementsContext";
import { PRESENTATION } from "./registry";

// Resolve a node that may be a plain element or a (state) => node render fn.
const resolve = (node, state) => (typeof node === "function" ? node(state) : node);

/**
 * Read the backend presentation for one capability and expose it as booleans.
 * Use this for fine-grained gating — e.g. disabling a single button under
 * READ_ONLY, or hiding one control — where wrapping in <Gate> is awkward.
 *
 * @param {string} capability  one of the authoritative backend capability keys
 * @returns {{
 *   capability: string, presentation: string|null, ready: boolean,
 *   isShow: boolean, isLock: boolean, isHidden: boolean,
 *   isReadOnly: boolean, needsConfig: boolean,
 *   visible: boolean, interactive: boolean, readOnly: boolean
 * }}
 */
export function useCapability(capability) {
  const { presentationFor, ready } = useEntitlements();
  const presentation = capability ? presentationFor(capability) : null;
  // null (unresolved) behaves like SHOW — see the module header.
  const effective = presentation || PRESENTATION.SHOW;
  return {
    capability,
    presentation,                                        // raw backend value or null
    ready: !!ready,
    isShow:      effective === PRESENTATION.SHOW,
    isLock:      effective === PRESENTATION.LOCK,
    isHidden:    effective === PRESENTATION.HIDE,
    isReadOnly:  effective === PRESENTATION.READ_ONLY,
    needsConfig: effective === PRESENTATION.CONFIGURATION_REQUIRED,
    // Derived affordances:
    visible:     effective !== PRESENTATION.HIDE,        // does it render at all?
    interactive: effective === PRESENTATION.SHOW,        // can the actor fully use it?
    readOnly:    effective === PRESENTATION.READ_ONLY,   // visible but not mutable
  };
}

/**
 * Gate a feature on a single capability.
 *
 * @param {string} capability            backend capability key
 * @param {React.ReactNode|Function} children  rendered on SHOW / READ_ONLY /
 *        safe-fallback. If a function, it receives the useCapability() state so
 *        the caller can disable mutating controls under READ_ONLY.
 * @param {React.ReactNode|Function} [lock]   override for LOCK (default: a
 *        neutral "not in your plan" card).
 * @param {React.ReactNode|Function} [setup] override for CONFIGURATION_REQUIRED
 *        (default: a neutral setup CTA).
 * @param {React.ReactNode|Function} [hidden] override for HIDE (default: null —
 *        renders nothing).
 * @param {Function} [render]  full manual control: (state) => node. Wins over
 *        every default above.
 */
export default function Gate({
  capability,
  children,
  lock,
  setup,
  hidden = null,
  render,
}) {
  const state = useCapability(capability);

  if (typeof render === "function") return resolve(render, state);

  if (state.isHidden) return resolve(hidden, state);

  if (state.isLock) {
    return lock !== undefined
      ? resolve(lock, state)
      : <FeatureLocked capability={capability} />;
  }

  if (state.needsConfig) {
    return setup !== undefined
      ? resolve(setup, state)
      : <FeatureSetupRequired capability={capability} />;
  }

  // SHOW, READ_ONLY, or safe-fallback (null projection): render the feature.
  return typeof children === "function" ? children(state) : (children ?? null);
}

// ── Default presentations ────────────────────────────────────────────────────
// Self-contained and neutral so the Gate has no hard dependency on the theme.
// Surfaces that want a themed card pass their own `lock` / `setup` node. Copy is
// deliberately plan-agnostic — no plan name is ever named in the frontend.

const CARD = {
  display: "flex", flexDirection: "column", alignItems: "center",
  justifyContent: "center", textAlign: "center", gap: 8,
  padding: "40px 28px", margin: "8px auto", maxWidth: 460,
  borderRadius: 14, border: "1px solid rgba(148,163,184,.35)",
  background: "rgba(148,163,184,.08)", color: "#475467",
};

export function FeatureLocked({ capability, title, message }) {
  return (
    <div role="note" data-entitlement="lock" data-capability={capability} style={CARD}>
      <div aria-hidden="true" style={{ fontSize: 26, lineHeight: 1 }}>🔒</div>
      <p style={{ fontWeight: 800, fontSize: 15, margin: 0, color: "#334155" }}>
        {title || "Not included in your plan"}
      </p>
      <p style={{ fontSize: 13, margin: 0, maxWidth: 360 }}>
        {message || "This feature is available on a higher plan. Upgrade to unlock it."}
      </p>
    </div>
  );
}

export function FeatureSetupRequired({ capability, title, message }) {
  return (
    <div role="note" data-entitlement="configuration_required" data-capability={capability}
      style={{ ...CARD, border: "1px solid rgba(245,158,11,.35)", background: "rgba(245,158,11,.08)", color: "#92400E" }}>
      <div aria-hidden="true" style={{ fontSize: 24, lineHeight: 1 }}>⚙️</div>
      <p style={{ fontWeight: 800, fontSize: 15, margin: 0, color: "#92400E" }}>
        {title || "Setup required"}
      </p>
      <p style={{ fontSize: 13, margin: 0, maxWidth: 360 }}>
        {message || "Finish configuring this feature to start using it."}
      </p>
    </div>
  );
}
