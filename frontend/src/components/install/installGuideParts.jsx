/**
 * Shared building blocks for the desktop install guides (Linux, macOS).
 *
 * Both guides are step lists containing terminal commands, so the numbered
 * step and the copy-to-clipboard command block live here rather than being
 * written twice. Keeping one implementation matters most for `Command`: it
 * carries the clipboard fallback, and a divergent second copy would quietly
 * lose it.
 */

import React, { useState } from "react";
import { Copy, Check } from "lucide-react";

/**
 * A numbered step with an icon rail.
 *
 * @param {object}  props
 * @param {number|string} props.n      step number shown as "Step N."
 * @param {Function} props.icon        lucide/react-icons component
 * @param {boolean} props.last         hides the connector line below
 * @param {"default"|"warning"} props.tone
 *        "warning" marks a step that only applies when something has gone
 *        wrong, so a reader can see at a glance it is not part of the happy
 *        path.
 */
export function Step({
  n, icon, title, body, children, last = false, tone = "default",
}) {
  // Capitalized local so JSX treats it as a component. Assigned rather than
  // destructured as `icon: Icon` because eslint-plugin-react is not installed
  // here, so a bare <Icon /> element name would not count as a usage and the
  // binding would be flagged unused. A capitalized *variable* is covered by the
  // config's varsIgnorePattern.
  const Icon = icon;
  const warn = tone === "warning";

  return (
    <section className="relative pl-12 pb-6">
      {!last && (
        <span aria-hidden className="absolute left-[19px] top-10 bottom-0 w-px bg-slate-200" />
      )}
      <span
        aria-hidden
        className={
          "absolute left-0 top-0 flex h-10 w-10 items-center justify-center rounded-full border " +
          (warn ? "border-amber-200 bg-amber-50" : "border-slate-200 bg-white")
        }
      >
        <Icon size={17} className={warn ? "text-amber-600" : "text-brand-blue"} />
      </span>
      <h2 className="text-base font-bold text-slate-900">
        <span className={"mr-1.5 " + (warn ? "text-amber-600" : "text-brand-blue")}>
          Step {n}.
        </span>
        {title}
      </h2>
      {body && <p className="mt-1 text-sm leading-relaxed text-slate-600">{body}</p>}
      {children && <div className="mt-3">{children}</div>}
    </section>
  );
}

/**
 * A terminal command with one-click copy.
 *
 * These have to be pasted into a terminal, so copying is the primary action —
 * retyping an `xattr` invocation by hand is how typos become support tickets.
 */
export function Command({ value, small = false, label }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      // The Clipboard API needs a secure context and permission. Fall back to a
      // hidden textarea so the button still works over plain http (e.g. an
      // internal host) instead of silently doing nothing.
      const ta = document.createElement("textarea");
      ta.value = value;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try { document.execCommand("copy"); } catch { /* nothing more to try */ }
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <div>
      {label && (
        <p className="mb-1.5 text-xs font-semibold text-slate-700">{label}</p>
      )}
      <div className="flex items-stretch gap-2">
        <code
          className={
            "flex-1 overflow-x-auto rounded-xl bg-slate-900 px-3 py-2.5 font-mono text-slate-100 " +
            (small ? "text-[11px]" : "text-xs sm:text-[13px]")
          }
        >
          <span aria-hidden className="mr-2 select-none text-slate-500">$</span>
          {value}
        </code>
        <button
          type="button"
          onClick={copy}
          aria-label={copied ? `Command copied: ${value}` : `Copy command: ${value}`}
          className="shrink-0 rounded-xl border border-slate-300 bg-white px-3 text-slate-600 transition-colors hover:bg-slate-50 hover:text-slate-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-blue/40"
        >
          {copied ? (
            <Check size={15} className="text-brand-green-dark" aria-hidden />
          ) : (
            <Copy size={15} aria-hidden />
          )}
          <span className="sr-only">{copied ? "Copied" : "Copy"}</span>
        </button>
      </div>
    </div>
  );
}

/** Page chrome shared by the install guides. */
export function GuideShell({ logo, icon, iconBg = "bg-slate-900", title, subtitle, children }) {
  const Icon = icon;
  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-3xl items-center justify-between px-5 py-4">
          <a href="/" aria-label="Temveris home">
            <img src={logo} alt="Temveris" className="h-7 w-auto" />
          </a>
          <a href="/" className="text-sm font-semibold text-brand-blue hover:underline">
            Sign in
          </a>
        </div>
      </header>

      <main className="mx-auto w-full max-w-3xl px-4 py-8 sm:px-5 sm:py-10">
        <div className="mb-6 flex items-center gap-3">
          <div className={"flex h-12 w-12 items-center justify-center rounded-2xl " + iconBg}>
            <Icon size={26} className="text-white" aria-hidden />
          </div>
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">{title}</h1>
            <p className="text-sm text-slate-500">{subtitle}</p>
          </div>
        </div>
        {children}

        <p className="mt-8 text-center text-xs text-slate-500">
          Stuck? Contact your Temveris administrator, or email{" "}
          <a href="mailto:support@temveris.example" className="font-semibold text-brand-blue hover:underline">
            support@temveris.example
          </a>
          .
        </p>
      </main>
    </div>
  );
}
