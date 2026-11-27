/**
 * Temveris for Linux — download + install guide.
 *
 * WHY LINUX GETS A PAGE INSTEAD OF A PLAIN DOWNLOAD
 * ------------------------------------------------
 * On Windows the installer is self-explanatory: download, double-click, done.
 * A `.deb` is not. Double-clicking one on most Ubuntu desktops opens Archive
 * Manager, which shows the package's internals and installs nothing — so a
 * download-only flow strands the user with a file they cannot obviously use.
 *
 * The install command is therefore the primary content here, not a footnote,
 * and it is copyable in one click because it has to be pasted into a terminal.
 *
 * Public route: no authentication. An employee who has just set their password
 * may not be signed in yet, and support will link people straight here.
 */

import React from "react";
import { FaLinux } from "react-icons/fa";
import { Download, Terminal, Rocket, LogIn, AlertTriangle } from "lucide-react";
import logo from "../assets/Temveris_Logo_PNG.png";
import { Step, Command, GuideShell } from "../components/install/installGuideParts";
import { DESKTOP_DOWNLOADS } from "../config";

const DEB_URL = DESKTOP_DOWNLOADS.linux;

// Verified against the published object: 74,541,716 bytes.
const SIZE_LABEL = "Ubuntu 22.04+ / Debian 12+ · 64-bit · 71 MB";

const INSTALL_CMD = "sudo apt install ~/Downloads/Temveris-linux.deb";
const FIX_DEPS_CMD = "sudo apt --fix-broken install";

export default function LinuxInstallGuide() {
  return (
    <GuideShell
      logo={logo}
      icon={FaLinux}
      title="Temveris for Linux"
      subtitle="Download and install the desktop tracker on Ubuntu or Debian."
    >
      {/* ── 1. Download ───────────────────────────────────────────── */}
      <Step
        n={1}
        icon={Download}
        title="Download the app"
        body="Save the package to your Downloads folder — the install command below expects it there."
      >
        <a
          href={DEB_URL}
          download
          rel="noopener"
          className="btn-hover inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand-blue px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-brand-blue-dark sm:w-auto"
        >
          <Download size={16} aria-hidden />
          Download for Linux (.deb)
        </a>
        <p className="mt-2 text-xs text-slate-500">{SIZE_LABEL}</p>
      </Step>

      {/* ── 2. Install ────────────────────────────────────────────── */}
      <Step
        n={2}
        icon={Terminal}
        title="Install the package"
        body="Open a terminal and run this. You'll be asked for your system password."
      >
        <Command value={INSTALL_CMD} />

        <div className="mt-3 flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-600" aria-hidden />
          <p className="text-xs leading-relaxed text-amber-900">
            <strong>Don't double-click the .deb file.</strong> On most Ubuntu
            desktops that opens Archive Manager, which only shows you what's
            inside the package — it doesn't install it. Use the command above.
          </p>
        </div>

        <details className="mt-3 rounded-xl border border-slate-200 bg-white p-3">
          <summary className="cursor-pointer text-xs font-semibold text-slate-700">
            Seeing a dependency error?
          </summary>
          <p className="mt-2 text-xs leading-relaxed text-slate-600">
            Run this, then repeat the install command:
          </p>
          <div className="mt-2">
            <Command value={FIX_DEPS_CMD} small />
          </div>
          <p className="mt-2 text-xs leading-relaxed text-slate-500">
            If you downloaded somewhere other than{" "}
            <code className="rounded bg-slate-100 px-1">~/Downloads</code>, swap
            that part of the path for wherever the file actually is.
          </p>
        </details>
      </Step>

      {/* ── 3. Launch ─────────────────────────────────────────────── */}
      <Step
        n={3}
        icon={Rocket}
        title="Launch Temveris"
        body="Press the Super key (the Windows key), type “temveris”, and open Temveris."
      />

      {/* ── 4. Sign in ────────────────────────────────────────────── */}
      <Step
        n={4}
        icon={LogIn}
        title="Sign in and clock in"
        body="Use the same email and password you set for your Temveris account, then press Clock In to start tracking."
        last
      />
    </GuideShell>
  );
}
