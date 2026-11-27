/**
 * ISO 3166-1 alpha-2 countries, derived from the browser's own data.
 *
 * The same approach lib/orgTime.js takes for timezones, and the same one
 * OrganizationSetup.jsx already uses to turn a stored code back into a name
 * (`Intl.DisplayNames(..., { type: "region" })`). Deriving the list means there
 * is no hand-maintained array to fall behind reality, and every entry is
 * guaranteed to be a code `Intl` recognises.
 *
 * The backend stores and validates only the two-letter code
 * (`OrgSettings.primary_country`, `^[A-Z]{2}$`), so the name here is purely
 * what the person reads — it localises to their browser for free.
 *
 * Note there is deliberately no filtering by "supported country". The platform
 * has no such list; the only country policy that exists is the sanctions screen
 * in app/commercial/fraud, applied server-side at signup.
 */

const A = "A".charCodeAt(0);

/** Every alpha-2 code Intl resolves to a real region name. */
function deriveCountries() {
  let display;
  try {
    display = new Intl.DisplayNames(undefined, { type: "region" });
  } catch {
    return [];
  }

  const out = [];
  for (let i = 0; i < 26; i++) {
    for (let j = 0; j < 26; j++) {
      const code = String.fromCharCode(A + i) + String.fromCharCode(A + j);
      let name;
      try {
        name = display.of(code);
      } catch {
        continue;
      }
      // Intl echoes the input back for codes it does not know, which is how an
      // unassigned pair like "XX" is told apart from a real one.
      if (!name || name === code) continue;

      // Skip withdrawn codes. Intl still resolves the historical ones — DD to
      // "Germany", UK to "United Kingdom", SU to "Russia", YU and CS both to
      // "Serbia" — so brute-forcing the alphabet produced fifteen names that
      // appeared twice in the list. Worse than the cosmetic repeat: choosing
      // the first "Germany" stored DD, which satisfies the backend's
      // ^[A-Z]{2}$ but is a code retired in 1990.
      //
      // `maximize()` resolves a region subtag to its current form, so a code
      // that is not its own canonical region is by definition superseded.
      // Derived rather than a hand-listed set of exceptions, for the same
      // reason the list itself is derived.
      let canonical;
      try {
        canonical = new Intl.Locale(`und-${code}`).maximize().region;
      } catch {
        canonical = code;
      }
      if (canonical && canonical !== code) continue;

      out.push({ code, name });
    }
  }
  return out.sort((a, b) => a.name.localeCompare(b.name));
}

let cached = null;

/** The full list, computed once per page load. */
export function listCountries() {
  if (cached === null) cached = deriveCountries();
  return cached;
}

/** Resolve a code to its display name, or null when it is not a real region. */
export function countryName(code) {
  const c = (code || "").trim().toUpperCase();
  if (c.length !== 2) return null;
  try {
    const name = new Intl.DisplayNames(undefined, { type: "region" }).of(c);
    return name && name !== c ? name : null;
  } catch {
    return null;
  }
}

/** Best-effort guess from the browser locale, e.g. "en-GB" → "GB". */
export function guessCountry() {
  try {
    const locale = new Intl.Locale(navigator.language);
    const region = locale.maximize?.().region || locale.region;
    return region && countryName(region) ? region : "";
  } catch {
    return "";
  }
}
