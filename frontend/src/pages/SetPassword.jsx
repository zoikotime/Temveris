import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Eye, EyeOff, User, Mail, CheckCircle, AlertCircle } from "lucide-react";
import { API_BASE as API } from "../config";
import AuthLayout from "../components/auth/AuthLayout";
import PasswordStrengthMeter from "../components/auth/PasswordStrengthMeter";
import { meetsAllPasswordRules } from "../components/auth/passwordRules";
import DesktopDownloadCards from "../components/auth/DesktopDownloadCards";

const PASSWORD_RULES = [
  { re: /.{8,}/,                  label: "At least 8 characters" },
  { re: /(?=.*[a-z])(?=.*[A-Z])/, label: "Uppercase and lowercase letters" },
  { re: /[0-9]/,                  label: "Number" },
  { re: /[@$!%*?&]/,              label: "Symbol (@$!%*?&)" },
];

const TERMS_VERSION   = "2026-05-01";
const PRIVACY_VERSION = "2026-05-01";

// The per-OS installer URLs now live with the cards that render them, in
// components/auth/DesktopDownloadCards.jsx.

export default function SetPassword() {
  const { token }  = useParams();
  const navigate   = useNavigate();

  const [step,      setStep]     = useState("loading"); // loading | form | done | error
  const [userInfo,  setUserInfo] = useState(null);

  const [fullName,   setFullName]  = useState("");
  const [pw,         setPw]        = useState("");
  const [confirm,    setConfirm]   = useState("");
  const [showPw,     setShowPw]    = useState(false);
  const [showCfm,    setShowCfm]   = useState(false);
  const [acceptTos,  setAcceptTos] = useState(false);
  const [submitting, setSub]       = useState(false);
  const [errMsg,     setErr]       = useState("");

  useEffect(() => {
    if (!token) { setStep("error"); return; }
    fetch(`${API}/auth/invite/verify/${token}`)
      .then(r => r.json())
      .then(d => {
        if (d.valid) {
          setUserInfo(d);
          setFullName(d.name || "");
          setStep("form");
        } else {
          setStep("error");
        }
      })
      .catch(() => setStep("error"));
  }, [token]);

  const allRulesMet    = meetsAllPasswordRules(pw, PASSWORD_RULES);
  const passwordsMatch = pw === confirm && pw.length > 0;
  const canSubmit      = fullName.trim().length >= 2 && allRulesMet && passwordsMatch && acceptTos;

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!canSubmit) return;
    setSub(true);
    setErr("");
    try {
      const res = await fetch(`${API}/auth/set-password`, {
        method:  "POST",
        headers: { "Content-Type": "application/json" },
        body:    JSON.stringify({ token, password: pw, name: fullName.trim() }),
      });
      const data = await res.json();
      if (!res.ok) { setErr(data.detail || "Something went wrong"); setSub(false); return; }

      const inElectronNow = !!(window.electronAuth || /Electron/i.test(navigator.userAgent || ""));

      if (inElectronNow) {
        localStorage.setItem("zt_token", data.access_token);
        localStorage.setItem("token",    data.access_token);
        localStorage.setItem("user_id",  String(data.user_id || data.user?.id || ""));
        localStorage.setItem("name",     data.name  || data.user?.name  || fullName.trim());
        localStorage.setItem("email",    data.email || data.user?.email || userInfo?.email || "");
        localStorage.setItem("role",     data.role  || data.user?.role  || "employee");

        if (window.electronAuth?.saveSession) {
          window.electronAuth.saveSession({
            token:   data.access_token,
            name:    data.name || data.user?.name || fullName.trim(),
            email:   data.email || data.user?.email || userInfo?.email || "",
            role:    data.role || data.user?.role || "employee",
            user_id: String(data.user_id || data.user?.id || ""),
          });
        }

        setStep("done");
        const role = data.role || data.user?.role || "employee";
        setTimeout(() => navigate(role === "admin" ? "/admin" : "/dashboard"), 1800);
      } else {
        setStep("done");
      }
    } catch {
      setErr("Network error. Please try again.");
      setSub(false);
    }
  };

  const inElectron = typeof window !== "undefined"
    && !!(window.electronAuth || /Electron/i.test(navigator.userAgent || ""));

  // ── Loading ───────────────────────────────────────────────────────────────
  if (step === "loading") return (
    <AuthLayout>
      <div className="py-8 flex flex-col items-center gap-3">
        <span className="loader" style={{ borderColor: "rgba(17,73,106,0.2)", borderTopColor: "#11496A" }} />
        <p className="text-slate-500 text-sm">Verifying invite link…</p>
      </div>
    </AuthLayout>
  );

  // ── Invalid / Expired ─────────────────────────────────────────────────────
  if (step === "error") return (
    <AuthLayout>
      <div className="flex justify-center mb-5">
        <div className="w-14 h-14 rounded-2xl bg-red-50 ring-1 ring-red-100 flex items-center justify-center">
          <AlertCircle className="text-red-500" size={26} />
        </div>
      </div>
      <div className="text-center space-y-3">
        <h1 className="text-[22px] font-bold tracking-tight text-slate-900">
          Invalid or expired link
        </h1>
        <p className="text-sm text-slate-500 leading-relaxed">
          This invite link is invalid or has expired (links expire after 24 hours).
          Ask your admin to resend the invitation.
        </p>
        <button
          onClick={() => navigate("/")}
          className="btn-hover w-full text-white py-3 rounded-xl text-[15px] font-semibold shadow-brandSoft hover:shadow-brand mt-2"
          style={{ background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)" }}
        >
          Back to sign in
        </button>
      </div>
    </AuthLayout>
  );

  // ── Done ──────────────────────────────────────────────────────────────────
  // Wider than the 400px form default: this screen holds three side-by-side
  // download cards, which at form width squeeze into unreadable slivers. The
  // form step below keeps the narrow width, where it is correct.
  if (step === "done") return (
    <AuthLayout maxWidth="max-w-[680px]">
      <div className="flex justify-center mb-6">
        <div className="w-16 h-16 rounded-2xl bg-brand-green/10 ring-1 ring-brand-green/20 flex items-center justify-center">
          <CheckCircle className="text-brand-green-dark" size={32} />
        </div>
      </div>
      <div className="text-center space-y-3">
        <h1 className="text-[26px] sm:text-[30px] font-bold tracking-tight text-slate-900">
          Account created
        </h1>
        <p className="mx-auto max-w-[46ch] text-[15px] text-slate-500 leading-relaxed">
          {inElectron
            ? "Your account is ready. Redirecting you to the dashboard shortly…"
            : "Your account is ready. Continue in the Temveris Desktop App to start tracking."}
        </p>

        <div className="bg-slate-50 border border-slate-200 rounded-2xl p-5 sm:p-7 text-left mt-6">
          <p className="text-base font-semibold text-slate-800 mb-1">
            {inElectron ? "Install the Desktop Tracker" : "Download the desktop app"}
          </p>
          <p className="text-sm text-slate-500 mb-5 leading-relaxed">
            {inElectron
              ? "Download the Temveris app to start tracking your work automatically. It runs silently in the background."
              : "Pick the installer for your operating system, then sign in with the email and password you just set."}
          </p>

          <DesktopDownloadCards />

          {!inElectron && (
            <p className="text-xs text-slate-400 mt-5 text-center">
              Already have the app? Open it and sign in with your email and password.
            </p>
          )}
        </div>

        {inElectron && (
          <p className="text-xs text-slate-400">Redirecting to your dashboard automatically…</p>
        )}
      </div>
    </AuthLayout>
  );

  // ── Sign-up Form ──────────────────────────────────────────────────────────
  return (
    <AuthLayout
      footer={
        <>
          Already have an account?{" "}
          <button
            onClick={() => navigate("/")}
            className="font-semibold text-brand-blue hover:underline underline-offset-2"
          >
            Sign in
          </button>
        </>
      }
    >
      <div className="text-center mb-7">
        <h1 className="text-[26px] sm:text-[28px] font-bold tracking-tight text-slate-900">
          Create your account
        </h1>
        <p className="mt-2 text-sm text-slate-500 leading-relaxed">
          Secure access to your organization's time and productivity platform.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4 stagger" noValidate>
        <div>
          <label
            htmlFor="zt-name"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            Full name
          </label>
          <div className="relative">
            <User
              className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none"
              size={18}
            />
            <input
              id="zt-name"
              type="text"
              placeholder="Alex Johnson"
              value={fullName}
              onChange={e => setFullName(e.target.value)}
              className="w-full bg-slate-50/70 border border-slate-200 rounded-xl pl-10 pr-4 py-3 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:bg-white"
              required
            />
          </div>
        </div>

        <div>
          <label
            htmlFor="zt-email"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            Work email
          </label>
          <div className="relative">
            <Mail
              className="absolute left-3.5 top-1/2 -translate-y-1/2 text-slate-400 pointer-events-none"
              size={18}
            />
            <input
              id="zt-email"
              type="email"
              value={userInfo?.email || ""}
              readOnly
              className="w-full bg-slate-100 border border-slate-200 rounded-xl pl-10 pr-4 py-3 text-[15px] text-slate-500 cursor-not-allowed"
            />
          </div>
        </div>

        <div>
          <label
            htmlFor="zt-pw"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            Password
          </label>
          <div className="relative">
            <input
              id="zt-pw"
              type={showPw ? "text" : "password"}
              placeholder="Create a strong password"
              value={pw}
              onChange={e => setPw(e.target.value)}
              className="w-full bg-slate-50/70 border border-slate-200 rounded-xl px-4 py-3 pr-11 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:bg-white"
              autoComplete="new-password"
              required
            />
            <button
              type="button"
              onClick={() => setShowPw(v => !v)}
              aria-label={showPw ? "Hide password" : "Show password"}
              className="absolute right-3 top-1/2 -translate-y-1/2 eye-btn text-slate-400 p-1"
            >
              {showPw ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>
        </div>

        <PasswordStrengthMeter value={pw} rules={PASSWORD_RULES} />

        <div>
          <label
            htmlFor="zt-confirm"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            Confirm password
          </label>
          <div className="relative">
            <input
              id="zt-confirm"
              type={showCfm ? "text" : "password"}
              placeholder="Re-enter your password"
              value={confirm}
              onChange={e => setConfirm(e.target.value)}
              className={`w-full bg-slate-50/70 border rounded-xl px-4 py-3 pr-11 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:bg-white ${
                confirm.length > 0
                  ? passwordsMatch ? "border-brand-green" : "border-red-400"
                  : "border-slate-200"
              }`}
              autoComplete="new-password"
              required
            />
            <button
              type="button"
              onClick={() => setShowCfm(v => !v)}
              aria-label={showCfm ? "Hide password" : "Show password"}
              className="absolute right-3 top-1/2 -translate-y-1/2 eye-btn text-slate-400 p-1"
            >
              {showCfm ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>
          {confirm.length > 0 && !passwordsMatch && (
            <p className="text-xs text-red-500 mt-1.5 ml-1">Passwords do not match</p>
          )}
        </div>

        <label className="flex items-start gap-2.5 text-[12.5px] text-slate-600 cursor-pointer select-none pt-1">
          <input
            type="checkbox"
            checked={acceptTos}
            onChange={e => setAcceptTos(e.target.checked)}
            className="mt-0.5 w-4 h-4 rounded accent-brand-green cursor-pointer"
          />
          <span className="leading-snug">
            I agree to Temveris's{" "}
            <a href="/terms" target="_blank" rel="noopener" className="font-semibold text-brand-blue hover:underline underline-offset-2">
              Terms of Service
            </a>
            {" "}and acknowledge the{" "}
            <a href="/privacy" target="_blank" rel="noopener" className="font-semibold text-brand-blue hover:underline underline-offset-2">
              Privacy Policy
            </a>.
          </span>
        </label>

        {errMsg && (
          <div className="bg-red-50 border border-red-200 rounded-xl px-4 py-3 text-sm text-red-600">
            {errMsg}
          </div>
        )}

        <button
          type="submit"
          disabled={submitting || !canSubmit}
          className="btn-hover w-full text-white py-3 rounded-xl text-[15px] font-semibold flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed shadow-brandSoft hover:shadow-brand mt-1"
          style={{ background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)" }}
        >
          {submitting ? (
            <>
              <span className="loader" />
              Creating account…
            </>
          ) : "Create account"}
        </button>

        <p className="text-[11px] text-slate-400 text-center leading-relaxed mt-1">
          By creating an account you accept Terms v{TERMS_VERSION} and Privacy v{PRIVACY_VERSION}.
        </p>
      </form>
    </AuthLayout>
  );
}
