import { useEffect } from "react";
import { NavLink, useLocation } from "react-router-dom";

// `/start` is the live, routed signup page. This previously pointed at
// `/register`, which has no route — so the tab fell through to the `*` wildcard
// and navigated straight back to the login screen. There is one signup surface;
// the duplicate Register page it pointed at has been removed.
//
// Each tab carries the dynamic import for its page. All three are code-split in
// App.jsx, so without this the first click on a tab suspends and the app-level
// fallback paints over the whole screen — the opposite of a seamless swap.
// Vite resolves these to the same chunks App.jsx requests, so warming them here
// costs one small fetch and makes the sibling routes instant.
const TABS = [
  { to: "/",                label: "Login",          load: () => import("../../pages/Login")          },
  { to: "/start",           label: "Sign Up",        load: () => import("../../pages/Start")          },
  { to: "/forgot-password", label: "Reset Password", load: () => import("../../pages/ForgotPassword") },
];

const prefetch = (load) => { load().catch(() => { /* offline — the route still works, just not instantly */ }); };

export default function AuthTabs() {
  const { pathname } = useLocation();

  // Warm the sibling chunks once the browser is idle, so a tab click has
  // nothing left to download. Falls back to a short timer where
  // requestIdleCallback is unavailable (Safari).
  useEffect(() => {
    const warm = () => TABS.filter((t) => t.to !== pathname).forEach((t) => prefetch(t.load));
    if (typeof window.requestIdleCallback === "function") {
      const id = window.requestIdleCallback(warm, { timeout: 2000 });
      return () => window.cancelIdleCallback?.(id);
    }
    const id = setTimeout(warm, 400);
    return () => clearTimeout(id);
  }, [pathname]);

  return (
    <div role="tablist" aria-label="Account" className="mb-5">
      <div className="flex items-end gap-1 border-b border-slate-200">
        {TABS.map((t) => {
          const active = pathname === t.to;
          return (
            <NavLink
              key={t.to}
              to={t.to}
              role="tab"
              aria-selected={active}
              onMouseEnter={() => prefetch(t.load)}
              onFocus={() => prefetch(t.load)}
              className={
                "relative px-4 sm:px-5 pb-2.5 pt-2 text-[14px] sm:text-[15px] font-semibold transition-colors " +
                (active
                  ? "text-brand-green-dark"
                  : "text-slate-500 hover:text-slate-700")
              }
            >
              {t.label}
              <span
                aria-hidden
                className={
                  "absolute left-2 right-2 -bottom-px h-[3px] rounded-t-full transition-all " +
                  (active ? "bg-brand-green-dark" : "bg-transparent")
                }
              />
            </NavLink>
          );
        })}
      </div>
    </div>
  );
}
