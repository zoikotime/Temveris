/**
 * Per-OS desktop app download cards.
 *
 * Shown after an account is created — on the employee invite page, and on the
 * self-service setup wizard's completion step.
 *
 * WHY EACH PLATFORM BEHAVES DIFFERENTLY
 * -------------------------------------
 * Windows downloads straight away: the .exe installer explains itself.
 *
 * Linux and macOS route to a guide instead, because for them the download is
 * the easy half. Double-clicking a .deb opens Archive Manager on most Ubuntu
 * desktops and installs nothing; a downloaded .dmg is often blocked by
 * Gatekeeper with "Temveris is damaged and can't be opened", which invites the
 * user to bin a working app. Handing over the file with no instructions turns
 * both into a dead end, so those cards say "Install guide" — the label matches
 * what the click actually does.
 *
 * Availability is a pure function of the config below: a platform with a
 * destination is offered, one without says "Coming soon". No probing, so
 * nothing here can hang or offer a link that turns out to 404.
 */

import React, { useMemo } from "react";
import { Link } from "react-router-dom";
import { FaWindows, FaApple, FaLinux } from "react-icons/fa";
import { Download, ArrowRight } from "lucide-react";
import { DESKTOP_DOWNLOADS } from "../../config";

// One of `directUrl` (download immediately) or `guidePath` (route to
// instructions). A platform with neither renders as "Coming soon".
const PLATFORMS = [
  {
    key: "win",
    label: "Windows",
    Icon: FaWindows,
    hint: "Windows 10 or later",
    cta: "Download",
    directUrl: DESKTOP_DOWNLOADS.win,
  },
  {
    key: "mac",
    label: "macOS",
    Icon: FaApple,
    hint: "macOS 12 or later",
    cta: "Install guide",
    guidePath: "/install/mac",
  },
  {
    key: "linux",
    label: "Linux",
    Icon: FaLinux,
    hint: "Ubuntu 22.04+ / Debian 12+",
    cta: "Install guide",
    guidePath: "/install/linux",
  },
];

/** Best-effort guess so we can highlight the visitor's own OS. */
function detectPlatform() {
  const ua = `${navigator.userAgent} ${navigator.platform || ""}`.toLowerCase();
  if (ua.includes("mac")) return "mac";
  if (ua.includes("linux") || ua.includes("x11") || ua.includes("android")) return "linux";
  if (ua.includes("win")) return "win";
  return null;
}

export default function DesktopDownloadCards({ className = "" }) {
  const mine = useMemo(() => detectPlatform(), []);

  return (
    <div className={className}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 sm:gap-4">
        {/* `p.Icon` rather than a destructured `Icon`: eslint-plugin-react is
            not installed here, so a bare <Icon /> element name is not counted
            as a usage and no-unused-vars flags it. */}
        {PLATFORMS.map((p) => {
          const { key, label, hint } = p;
          const isGuide = Boolean(p.guidePath);
          const available = Boolean(p.directUrl || p.guidePath);
          const isMine = mine === key;
          const CtaIcon = isGuide ? ArrowRight : Download;

          const cardClass =
            "group flex min-h-[168px] flex-col items-center justify-center gap-2 rounded-2xl " +
            "border p-5 text-center " +
            "transition-colors focus-visible:outline-none focus-visible:ring-2 " +
            "focus-visible:ring-brand-blue/40 " +
            (isMine
              ? "border-brand-green bg-brand-green-light/60 hover:bg-brand-green-light"
              : "border-slate-200 bg-white hover:border-slate-300 hover:bg-slate-50");

          const inner = (
            <>
              <p.Icon
                size={30}
                aria-hidden
                className={isMine ? "text-brand-green-dark" : "text-slate-700"}
              />
              <span className="text-[15px] font-semibold text-slate-900">{label}</span>
              <span className="text-[11.5px] leading-snug text-slate-500">{hint}</span>
              <span
                className={
                  "mt-1.5 inline-flex items-center gap-1 text-[13px] font-semibold " +
                  (isMine ? "text-brand-green-dark" : "text-brand-blue")
                }
              >
                <CtaIcon size={14} aria-hidden />
                {p.cta || "Download"}
              </span>
              {isMine && (
                <span className="text-[10px] font-semibold uppercase tracking-wide text-brand-green-dark">
                  Your device
                </span>
              )}
            </>
          );

          if (isGuide) {
            // Internal navigation — a router Link, so it does not reload the app.
            return (
              <Link
                key={key}
                to={p.guidePath}
                aria-label={`Temveris for ${label} — download and install guide`}
                className={cardClass}
              >
                {inner}
              </Link>
            );
          }

          if (available) {
            return (
              <a
                key={key}
                href={p.directUrl}
                // No target="_blank": the installer is served as a binary
                // content type, so the browser downloads it in place. Opening a
                // tab would flash a blank window that immediately closes.
                // `download` is a hint only — browsers ignore it cross-origin,
                // which is fine because the content type already forces a
                // download rather than a navigation.
                download
                rel="noopener"
                aria-label={`Download Temveris for ${label}`}
                className={cardClass}
              >
                {inner}
              </a>
            );
          }

          return (
            <div
              key={key}
              aria-disabled="true"
              className="flex min-h-[168px] flex-col items-center justify-center gap-2 rounded-2xl border border-dashed border-slate-200 bg-slate-50/70 p-5 text-center"
            >
              <p.Icon size={30} aria-hidden className="text-slate-300" />
              <span className="text-sm font-semibold text-slate-400">{label}</span>
              <span className="text-[11px] text-slate-400">{hint}</span>
              <span className="mt-1 text-xs font-medium text-slate-400">Coming soon</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
