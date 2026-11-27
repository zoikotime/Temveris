/**
 * The chrome every activation screen wears: the Temveris mark, and the
 * four-step indicator.
 *
 * Account, Verify, Workspace, Ready and the subscription configurator all
 * opened with a bare progress indicator and no branding, so the flow read as a
 * detached form rather than as part of the product — the first screen a
 * customer ever sees, with nothing on it saying whose it is.
 *
 * The two pieces are combined here rather than added to `ActivationProgress`
 * because they answer different questions ("where am I in this flow" versus
 * "whose product is this"), and rather than pasted into each page because
 * there are five of them and they would drift.
 *
 * The mark is deliberately NOT a link. Every screen after Account sits inside
 * a flow with server-side state behind it; a logo that quietly navigates to
 * the marketing or login page is a trapdoor out of a half-finished signup.
 */

import React from "react";

import logo from "../../assets/Temveris_Logo_PNG.png";
import ActivationProgress from "./ActivationProgress";

export default function ActivationHeader({ current = 1, steps = true, className = "" }) {
  return (
    // Rendered OUTSIDE each page's centred `max-w-*` column, directly in the
    // padded page. That is what makes `left-0` the corner of the page rather
    // than the left edge of the content column — which sits well inside the
    // viewport on a wide screen, and on the narrower Ready layout put the mark
    // directly on top of the "Account" step.
    //
    // The steps stay centred on the viewport, which is where they already
    // appeared: every column is itself centred, so nothing moves but the logo.
    <header
      className={
        "relative flex flex-col items-center gap-3 sm:block " +
        // With the steps hidden the logo is the only child, and from `sm` up it
        // is absolutely positioned — so the header would collapse to nothing
        // and the page content would ride up beside the mark. Reserve its
        // height explicitly in that case.
        (steps ? "" : "sm:h-9 ") +
        className
      }
    >
      {/* Stacked above the steps on narrow screens, in the top-left corner
          from `sm` up — where the centred indicator leaves room beside it. */}
      <img
        src={logo}
        alt="Temveris"
        draggable={false}
        className="h-7 w-auto select-none sm:absolute sm:left-0 sm:top-0.5 sm:h-8"
      />
      {/* `steps={false}` for surfaces that wear the same chrome but are not
          part of the four-step flow — the post-payment confirmation, which
          happens after activation has already finished. Showing a progress
          indicator there would invent a step that does not exist. */}
      {steps && <ActivationProgress current={current} />}
    </header>
  );
}
