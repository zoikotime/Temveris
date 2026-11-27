/**
 * Temveris for macOS — download + install guide.
 *
 * WHY macOS GETS A PAGE INSTEAD OF A PLAIN DOWNLOAD
 * ------------------------------------------------
 * A .dmg needs the user to mount it and drag the app to Applications, and on
 * many Macs Gatekeeper then refuses to open it with the alarming and misleading
 * message "Temveris is damaged and can't be opened. You should move it to the
 * Bin." Nothing is damaged — the download simply carries the
 * com.apple.quarantine attribute. Handing someone the file with no guidance
 * makes the app look broken and invites them to delete it.
 *
 * KNOWN PRODUCTION GAP
 * --------------------
 * The `xattr -dr com.apple.quarantine` step below is a workaround, not a fix.
 * The real fix is to Apple code-sign and notarize the build so it opens
 * normally, with no Terminal at all. That is a release-pipeline change, tracked
 * outside this page; until it lands, the troubleshooting steps here are what
 * stand between a Mac user and a dead end.
 *
 * Public route: no authentication. An employee who has just set their password
 * may not be signed in yet, and support will link people straight here.
 */

import React from "react";
import { FaApple } from "react-icons/fa";
import {
  Download, FolderOpen, MoveRight, Rocket, ShieldAlert, LifeBuoy, AlertTriangle,
} from "lucide-react";
import logo from "../assets/Temveris_Logo_PNG.png";
import { Step, Command, GuideShell } from "../components/install/installGuideParts";
import { DESKTOP_DOWNLOADS } from "../config";

const DMG_URL = DESKTOP_DOWNLOADS.mac;

// Verified against the published object: 98,300,062 bytes.
const SIZE_LABEL = "macOS 12 or later · Apple silicon & Intel · 94 MB";

const QUARANTINE_APPLICATIONS =
  "xattr -dr com.apple.quarantine /Applications/Temveris.app";
const QUARANTINE_DOWNLOADS =
  "xattr -dr com.apple.quarantine ~/Downloads/Temveris.app";
const OPEN_CMD = "open /Applications/Temveris.app";
const SPCTL_CMD = "spctl -a -vv /Applications/Temveris.app";

export default function MacInstallGuide() {
  return (
    <GuideShell
      logo={logo}
      icon={FaApple}
      title="Temveris for macOS"
      subtitle="Download and install the desktop tracker on your Mac."
    >
      {/* ── 1. Download ───────────────────────────────────────────── */}
      <Step
        n={1}
        icon={Download}
        title="Download Temveris"
        body="Save the disk image to your Downloads folder and wait for it to finish."
      >
        <a
          href={DMG_URL}
          download
          rel="noopener"
          className="btn-hover inline-flex w-full items-center justify-center gap-2 rounded-xl bg-brand-blue px-5 py-3 text-sm font-semibold text-white transition-colors hover:bg-brand-blue-dark sm:w-auto"
        >
          <Download size={16} aria-hidden />
          Download for macOS (.dmg)
        </a>
        <p className="mt-2 text-xs text-slate-500">{SIZE_LABEL}</p>
      </Step>

      {/* ── 2. Open the DMG ───────────────────────────────────────── */}
      <Step
        n={2}
        icon={FolderOpen}
        title="Open the disk image"
        body="Open your Downloads folder and double-click Temveris.dmg. An installation window opens."
      />

      {/* ── 3. Move to Applications ───────────────────────────────── */}
      <Step
        n={3}
        icon={MoveRight}
        title="Move Temveris to Applications"
        body="In that window, drag the Temveris app onto the Applications folder and wait for the copy to finish. You can then eject the Temveris disk image from Finder."
      >
        <div className="rounded-xl border border-slate-200 bg-white p-3">
          <p className="text-xs leading-relaxed text-slate-600">
            Running it straight from the disk image works, but it stops working
            as soon as the image is ejected — copy it to Applications so it stays
            installed.
          </p>
        </div>
      </Step>

      {/* ── 4. Quarantine — only if blocked ───────────────────────── */}
      <Step
        n={4}
        icon={ShieldAlert}
        tone="warning"
        title="Only if macOS says the app is damaged"
        body="Skip this step unless you see the message below. Most Macs open Temveris without it."
      >
        <div className="flex gap-2 rounded-xl border border-amber-200 bg-amber-50 p-3">
          <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-600" aria-hidden />
          <div className="text-xs leading-relaxed text-amber-900">
            <p className="font-semibold">
              “Temveris” is damaged and can’t be opened. You should move it to
              the Bin.
            </p>
            <p className="mt-1">
              Nothing is actually damaged. macOS flags apps downloaded from the
              internet with a quarantine attribute, and Gatekeeper is refusing to
              open it. Removing that flag fixes it — <strong>don’t move it to the
              Bin and don’t reinstall.</strong>
            </p>
          </div>
        </div>

        <p className="mt-3 text-xs leading-relaxed text-slate-600">
          Open <strong>Applications → Utilities → Terminal</strong>, then run:
        </p>
        <div className="mt-2">
          <Command value={QUARANTINE_APPLICATIONS} />
        </div>

        <details className="mt-3 rounded-xl border border-slate-200 bg-white p-3">
          <summary className="cursor-pointer text-xs font-semibold text-slate-700">
            Haven’t moved it to Applications yet?
          </summary>
          <p className="mt-2 text-xs leading-relaxed text-slate-600">
            If the app is still in Downloads, run this instead:
          </p>
          <div className="mt-2">
            <Command value={QUARANTINE_DOWNLOADS} small />
          </div>
        </details>
      </Step>

      {/* ── 5. Open ───────────────────────────────────────────────── */}
      <Step
        n={5}
        icon={Rocket}
        title="Open Temveris"
        body="Open Finder → Applications and double-click Temveris. Then sign in with your Temveris email and password and press Clock In."
      >
        <details className="rounded-xl border border-slate-200 bg-white p-3">
          <summary className="cursor-pointer text-xs font-semibold text-slate-700">
            Prefer to open it from Terminal?
          </summary>
          <div className="mt-2">
            <Command value={OPEN_CMD} small />
          </div>
        </details>
      </Step>

      {/* ── 6. Still blocked ─────────────────────────────────────── */}
      <Step
        n={6}
        icon={LifeBuoy}
        tone="warning"
        title="Still won’t open?"
        body="Don’t reinstall repeatedly — it won’t change the outcome. Run this and send the output to your Temveris administrator so the app's signing status can be checked."
        last
      >
        <Command value={SPCTL_CMD} />
      </Step>
    </GuideShell>
  );
}
