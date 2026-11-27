/**
 * Persistent shell for the three tabbed auth routes (/, /start,
 * /forgot-password).
 *
 * Previously each of those pages rendered its own <AuthLayout>, so switching
 * tabs tore the whole surface down and built it again: the gradient background,
 * the card, the logo and the tab bar all remounted and replayed the 450ms
 * `page-enter` animation, the lazy chunk flashed a full-screen white spinner in
 * between, and the card height snapped from one page's to the next's.
 *
 * Mounting the chrome once and swapping only the <Outlet /> keeps the logo and
 * tabs perfectly still — the only things that move are the parts that actually
 * differ. Two supporting pieces make that hold:
 *
 *   - PageTransition groups these paths under a single transition key, so the
 *     route swap above us doesn't unmount this component.
 *   - AuthTabs prefetches the sibling chunks, so the local <Suspense> below
 *     almost never has to show anything.
 */

import { Suspense, useLayoutEffect, useRef, useState } from "react";
import { Outlet, useLocation } from "react-router-dom";
import AuthLayout from "./AuthLayout";
import AuthTabs from "./AuthTabs";
import AuthHeroPanel from "./AuthHeroPanel";

export default function AuthShell() {
  const { pathname } = useLocation();

  // Animate the card's height between pages instead of letting it jump. The
  // outer div owns the transitioning height; the inner one is measured at its
  // natural size.
  // Starts as null so the card is auto-height on first paint: the first
  // measurement then pins it to the height it already has, which is not a
  // visible change and so animates nothing. Only a later content swap moves it.
  const inner = useRef(null);
  const [height, setHeight] = useState(null);

  useLayoutEffect(() => {
    const el = inner.current;
    if (!el || typeof ResizeObserver === "undefined") return;

    const measure = () => setHeight(el.offsetHeight);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  return (
    <AuthLayout maxWidth="max-w-[440px]" footer={null} aside={<AuthHeroPanel />}>
      <AuthTabs />

      <div
        style={{
          height: height ?? undefined,
          transition: "height 340ms cubic-bezier(0.22, 1, 0.36, 1)",
        }}
        className="overflow-hidden"
      >
        <div ref={inner}>
          {/* Keyed on the path so each page fades in on its own. The local
              boundary keeps the shell painted while a cold chunk loads,
              instead of the app-level full-screen fallback taking over. */}
          <div key={pathname} className="auth-swap">
            <Suspense fallback={<AuthSwapFallback />}>
              <Outlet />
            </Suspense>
          </div>
        </div>
      </div>
    </AuthLayout>
  );
}

function AuthSwapFallback() {
  return (
    <div className="flex items-center justify-center py-20">
      <span className="loader" />
    </div>
  );
}
