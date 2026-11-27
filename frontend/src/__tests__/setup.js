// jsdom side-effects that tests rely on. Kept light — Jest-dom only.
import "@testing-library/jest-dom";

// Stub minimal localStorage / sessionStorage in case jsdom's are missing on
// some CI images.
if (typeof globalThis.sessionStorage === "undefined") {
  let store = {};
  globalThis.sessionStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { store = {}; },
  };
}
