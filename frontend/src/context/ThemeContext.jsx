/**
 * Temveris — Global Theme Context
 * ================================
 * Provides a single source of truth for light / dark mode across the
 * entire application. Persists the user's choice in localStorage so it
 * survives page reloads, and exposes a unified `C` color object that
 * every page can consume directly.
 *
 *   const { isDark, toggleDark, C } = useTheme();
 *
 * Pages no longer need their own local `const C = {...}` palette —
 * just destructure `C` from the hook and you'll get the right colors
 * for the active mode.
 */
import React, { createContext, useContext, useState, useEffect, useCallback, useMemo } from "react";

// ── Unified palette ──────────────────────────────────────────────────────────
// Single factory used by every page in the app. Add a key here when a new
// page needs a colour and the matching dark variant.
export const makeC = (dark) => ({
  // surfaces & text — dark mode uses neutral near-black greys (no blue cast)
  // so the dashboard reads as black-on-black rather than slate-blue. Light
  // mode is unchanged.
  bg:           dark ? "#0A0A0A" : "#F5F7FA",
  surface:      dark ? "#141414" : "#FFFFFF",
  surfaceAlt:   dark ? "#0F0F0F" : "#F8FAFC",
  // Alias kept for components that read `C.surface2` directly — the key
  // didn't exist on the global palette so those components fell back to
  // hard-coded light greys (#F8FAFC) and became unreadable in dark mode.
  // (Activity Log timeline rows, Leave Management cards, etc.)
  surface2:     dark ? "#0F0F0F" : "#F8FAFC",
  border:       dark ? "#262626" : "#E4E7EC",
  borderLight:  dark ? "#1A1A1A" : "#F2F4F7",
  text:         dark ? "#F5F5F5" : "#101828",
  textSub:      dark ? "#A3A3A3" : "#475467",
  textLight:    dark ? "#737373" : "#98A2B3",
  // back-compat alias used by older pages (LeavePage / AdminLeave)
  sub:          dark ? "#A3A3A3" : "#475467",

  // brand — logo dark blue (#11496A)
  brand:        dark ? "#3D8AB5" : "#11496A",
  brandDark:    dark ? "#2A6F94" : "#0E3B57",
  brandBg:      dark ? "#0E2B3D" : "#E7F0F6",
  brandBdr:     dark ? "#1F3D55" : "#A6C7DC",
  // logo green accent (#44B97B) — pairs with the logo's blue
  brandAccent:     dark ? "#5FCC93" : "#44B97B",
  brandAccentDark: dark ? "#3FAF77" : "#2F8F5C",
  brandAccentBg:   dark ? "#0D2A1A" : "#E8F7EE",

  // success / green — aligned with logo green
  green:        dark ? "#5FCC93" : "#2F8F5C",
  greenBg:      dark ? "#0D2A1A" : "#E8F7EE",
  greenBdr:     dark ? "#1F5A3D" : "#A8E0BD",
  success:      dark ? "#5FCC93" : "#2F8F5C",
  successBg:    dark ? "#0D2A1A" : "#E8F7EE",
  successBorder: dark ? "#1F5A3D" : "#A8E0BD",

  // amber / warn
  amber:        dark ? "#FBBF24" : "#B54708",
  amberBg:      dark ? "#3D2A0A" : "#FFFAEB",
  amberBdr:     dark ? "#92400E" : "#FEDF89",
  warn:         dark ? "#FB923C" : "#C2410C",
  warnBg:       dark ? "#3D2A0A" : "#FFF7ED",
  warnBorder:   dark ? "#92400E" : "#FED7AA",

  // red / error
  red:          dark ? "#F87171" : "#B42318",
  redBg:        dark ? "#3D1F1A" : "#FEF3F2",
  redBdr:       dark ? "#991B1B" : "#FECDCA",
  error:        dark ? "#F87171" : "#B91C1C",
  errorBg:      dark ? "#3D1F1A" : "#FEF2F2",
  errorBorder:  dark ? "#991B1B" : "#FCA5A5",

  // aliases kept for back-compat with module-level C objects
  danger:       dark ? "#F87171" : "#B42318",
  dangerBg:     dark ? "#3D1F1A" : "#FEF3F2",
  purple:       dark ? "#9F7AEA" : "#6941C6",
  purpleBg:     dark ? "#2D1B69" : "#F4F0FF",

  // shape tokens
  r:   10,
  rSm: 6,
});

// ── Context ──────────────────────────────────────────────────────────────────
const ThemeContext = createContext({
  isDark: false,
  toggleDark: () => {},
  setDark: () => {},
  C: makeC(false),
});

const STORAGE_KEY = "zt_theme_dark";

const readInitial = () => {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw === "true")  return true;
    if (raw === "false") return false;
  } catch {}
  return false;
};

export function ThemeProvider({ children }) {
  const [isDark, setIsDark] = useState(readInitial);

  // Persist + broadcast across tabs so opening Settings in a new tab
  // picks up the same mode the admin set on the dashboard.
  useEffect(() => {
    try { localStorage.setItem(STORAGE_KEY, String(isDark)); } catch {}
  }, [isDark]);

  // Mirror the React-side `isDark` flag onto <html> so global CSS can target
  // dark mode without having to thread `isDark` through every component.
  // `color-scheme` is the standard CSS property the browser uses to decide
  // whether native controls (date picker popup, scrollbars, form indicators)
  // render in light or dark — without it the calendar icon on <input
  // type="date"> renders solid black and disappears against a dark input.
  useEffect(() => {
    const el = document.documentElement;
    if (isDark) {
      el.setAttribute("data-theme", "dark");
      el.style.colorScheme = "dark";
    } else {
      el.removeAttribute("data-theme");
      el.style.colorScheme = "light";
    }
  }, [isDark]);

  // Listen for changes from other tabs.
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === STORAGE_KEY) {
        setIsDark(e.newValue === "true");
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const toggleDark = useCallback(() => setIsDark(d => !d), []);
  const setDark    = useCallback((v) => setIsDark(!!v), []);

  const value = useMemo(() => ({
    isDark,
    toggleDark,
    setDark,
    C: makeC(isDark),
  }), [isDark, toggleDark, setDark]);

  return (
    <ThemeContext.Provider value={value}>
      {children}
    </ThemeContext.Provider>
  );
}

export function useTheme() {
  return useContext(ThemeContext);
}
