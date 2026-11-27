import { CSSTransition, SwitchTransition } from "react-transition-group";
import { useLocation } from "react-router-dom";
import { useRef } from "react";

// IMPORTANT: this wrapper uses SwitchTransition with mode="out-in" so the
// previous route is fully unmounted *before* the next one mounts. The earlier
// TransitionGroup version kept both pages mounted for the timeout window,
// which caused floating fixed-position elements (e.g. the Dashboard
// "Temveris" timer pill at bottom-right) to render twice during navigation.
// `exit: 0` keeps the swap perceptually instant; `enter: 450` matches the
// .page-enter-active CSS animation duration in index.css.
// The tabbed auth routes share one persistent shell (components/auth/AuthShell)
// so switching between Login / Sign Up / Reset Password swaps only the form,
// leaving the card, logo and tab bar untouched. Keying the transition on the
// raw pathname would defeat that: this wrapper would unmount the shell and
// remount it on every tab click. Collapsing the group to a single key lets
// React keep the shell mounted, while any navigation *out* of the group still
// animates normally.
const AUTH_GROUP = new Set(["/", "/start", "/forgot-password"]);
const transitionKey = (pathname) => (AUTH_GROUP.has(pathname) ? "auth" : pathname);

function PageTransition({ children }) {

  const location = useLocation();
  const nodeRef = useRef(null);

  return (
    <SwitchTransition mode="out-in">
      <CSSTransition
        key={transitionKey(location.pathname)}
        nodeRef={nodeRef}
        timeout={{ enter: 450, exit: 0 }}
        classNames="page"
        unmountOnExit
      >
        <div ref={nodeRef}>
          {children}
        </div>
      </CSSTransition>
    </SwitchTransition>
  );
}

export default PageTransition;