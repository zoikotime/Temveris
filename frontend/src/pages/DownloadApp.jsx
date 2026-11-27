/**
 * Desktop app download page — public route `/download`.
 *
 * The same per-OS cards an employee sees right after setting their password,
 * on a page anyone can reach at any time. The admin header links here so an
 * admin who is already signed in (and therefore never passes through the
 * "Account created" screen) still has a way to get the installer, and support
 * can hand out one stable link.
 *
 * Public on purpose, matching /install/linux and /install/mac: the cards it
 * renders link into those guides, and requiring a session to reach a download
 * page would lock out exactly the people who have not signed in yet.
 */

import React from "react";
import { useNavigate } from "react-router-dom";
import { ArrowLeft, MonitorDown } from "lucide-react";
import AuthLayout from "../components/auth/AuthLayout";
import DesktopDownloadCards from "../components/auth/DesktopDownloadCards";

export default function DownloadApp() {
  const navigate = useNavigate();

  // Going back is right when the visitor arrived from inside the app (the
  // admin header button). Opened cold from a support link there is nothing to
  // go back to, so fall back to the sign-in page rather than leaving the
  // browser on this page or bouncing out of the app entirely.
  const goBack = () => {
    if (window.history.length > 1) navigate(-1);
    else navigate("/");
  };

  return (
    // Wider than the 400px form default — three side-by-side cards squeeze
    // into unreadable slivers at form width.
    <AuthLayout maxWidth="max-w-[680px]">
      <div className="flex justify-center mb-6">
        <div className="w-16 h-16 rounded-2xl bg-brand-green/10 ring-1 ring-brand-green/20 flex items-center justify-center">
          <MonitorDown className="text-brand-green-dark" size={32} />
        </div>
      </div>

      <div className="text-center space-y-3">
        <h1 className="text-[26px] sm:text-[30px] font-bold tracking-tight text-slate-900">
          Download Temveris
        </h1>
        <p className="mx-auto max-w-[46ch] text-[15px] text-slate-500 leading-relaxed">
          Install the Temveris Desktop App to track work automatically. It runs
          quietly in the background once you sign in.
        </p>

        <div className="bg-slate-50 border border-slate-200 rounded-2xl p-5 sm:p-7 text-left mt-6">
          <p className="text-base font-semibold text-slate-800 mb-1">
            Download the desktop app
          </p>
          <p className="text-sm text-slate-500 mb-5 leading-relaxed">
            Pick the installer for your operating system, then sign in with your
            Temveris email and password.
          </p>

          <DesktopDownloadCards />

          <p className="text-xs text-slate-400 mt-5 text-center">
            Already have the app? Open it and sign in with your email and password.
          </p>
        </div>

        <div className="pt-1">
          <button
            type="button"
            onClick={goBack}
            className="inline-flex items-center gap-1.5 text-[13px] font-semibold text-brand-blue hover:underline underline-offset-2"
          >
            <ArrowLeft size={15} aria-hidden />
            Back
          </button>
        </div>
      </div>
    </AuthLayout>
  );
}
