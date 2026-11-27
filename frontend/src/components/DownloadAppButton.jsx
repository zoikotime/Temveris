/**
 * "Download App" header CTA — shared by the admin and employee dashboards.
 *
 * WHY IT EXISTS
 * -------------
 * The per-OS installers are only offered on the "Account created" screen, at
 * the end of the invite / signup flow. Anyone who is already signed in never
 * passes through it again, so without this button there is no route to the
 * desktop app from inside the product.
 *
 * It sends users to the shared /download page rather than linking a single
 * installer: the right file depends on their OS, and for macOS and Linux the
 * right destination is an install guide, not a download.
 *
 * The look (gradient fill, drifting sheen, pinging halo, dipping arrow) lives
 * in index.css under `.zt-dl-btn` — keyframes and a pseudo-element cannot be
 * expressed inline, and both dashboards render this, so duplicating them in two
 * inline <style> blocks would guarantee they drift apart.
 */

import React from "react";
import { useNavigate } from "react-router-dom";
import { Download } from "lucide-react";

// Inside the Electron shell the desktop app is already installed and running,
// so offering to download it is a dead end. Same detection the invite flow uses
// (`window.isDesktopApp` is set by the preload script; the other two cover
// older shells that predate it).
//
// Read at render rather than module load: the value cannot change during a
// session, but a module-level constant is baked in at import time and there is
// nothing to gain from that here.
const inDesktopShell = () =>
  typeof window !== "undefined" &&
  (window.isDesktopApp === true ||
    !!window.electronAuth ||
    /Electron/i.test(window.navigator?.userAgent || ""));

export default function DownloadAppButton({
  label = "Download App",
  title,
  // "sm" matches the employee dashboard's smaller header controls; the admin
  // header's controls are the larger default.
  size = "md",
  // "solid" is the admin header's flat green button (Live Workforce Overview
  // design); the default keeps the animated gradient the employee header uses.
  variant,
  // Colour showing through the focus ring's inner gap — pass the header's
  // surface so the ring stays legible in dark mode.
  gapColor,
  style,
}) {
  const navigate = useNavigate();

  if (inDesktopShell()) return null;

  const className = ["zt-dl-btn", size === "sm" && "zt-dl-btn--sm", variant === "solid" && "zt-dl-btn--solid"]
    .filter(Boolean).join(" ");

  return (
    <button
      type="button"
      className={className}
      onClick={() => navigate("/download")}
      title={title || label}
      style={gapColor ? { "--zt-dl-gap": gapColor, ...style } : style}
    >
      {/* lucide strokes with currentColor, so the arrow follows the label. */}
      <span className="zt-dl-ic"><Download size={14} aria-hidden /></span>
      {label}
    </button>
  );
}
