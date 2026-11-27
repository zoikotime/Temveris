/**
 * ResetPassword — OTP flow, step 3 of 3.
 *
 * Reached from /verify-otp via router state ({ email, otpVerified: true }).
 * If the user lands here without that state we bounce to /forgot-password —
 * the server will reject the call anyway (no verified OTP row), so it's
 * pointless to let them type the password first.
 *
 * On success the user is redirected to /login and must sign in with the
 * new password — no auto-login. This is what the spec asks for and it
 * also makes the user prove they actually know what they just set.
 */
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { Eye, EyeOff, Lock, CheckCircle, ChevronLeft } from "lucide-react";
import toast from "react-hot-toast";
import { resetPassword } from "../api/authApi";
import AuthLayout from "../components/auth/AuthLayout";
import ResetStepper from "../components/auth/ResetStepper";
import PasswordStrengthMeter from "../components/auth/PasswordStrengthMeter";
import { meetsAllPasswordRules } from "../components/auth/passwordRules";

// Backend policy — keep aligned with backend validators.
const PASSWORD_RULES = [
  { re: /.{8,}/,                  label: "At least 8 characters" },
  { re: /(?=.*[a-z])(?=.*[A-Z])/, label: "Uppercase and lowercase letters" },
  { re: /[0-9]/,                  label: "Number" },
  { re: /[@$!%*?&]/,              label: "Symbol (@$!%*?&)" },
];

function ResetPassword() {
  const navigate  = useNavigate();
  const location  = useLocation();

  const email       = location.state?.email || "";
  const otpVerified = !!location.state?.otpVerified;

  const [step, setStep]       = useState("form"); // "form" | "done"
  const [pw, setPw]           = useState("");
  const [confirm, setConfirm] = useState("");
  const [showPw, setShowPw]   = useState(false);
  const [loading, setLoading] = useState(false);

  const allRulesMet    = meetsAllPasswordRules(pw, PASSWORD_RULES);
  const passwordsMatch = pw === confirm && pw.length > 0;

  useEffect(() => {
    if (!email || !otpVerified) {
      navigate("/forgot-password", { replace: true });
    }
  }, [email, otpVerified, navigate]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!allRulesMet || !passwordsMatch) return;

    try {
      setLoading(true);
      await resetPassword({ email, newPassword: pw });

      toast.success("Your password has been updated successfully.");
      setStep("done");

      setTimeout(() => {
        navigate("/", { replace: true });
      }, 2500);

    } catch (error) {
      toast.error(error.response?.data?.detail || "Something went wrong ❌");
    } finally {
      setLoading(false);
    }
  };

  if (step === "done") return (
    <AuthLayout>
      <ResetStepper current={2} />
      <div className="flex justify-center mb-5">
        <div className="w-14 h-14 rounded-2xl bg-brand-green/10 ring-1 ring-brand-green/20 flex items-center justify-center">
          <CheckCircle className="text-brand-green-dark" size={26} />
        </div>
      </div>
      <div className="text-center space-y-2">
        <h1 className="text-[24px] sm:text-[26px] font-bold tracking-tight text-slate-900">
          Password updated
        </h1>
        <p className="text-sm text-slate-500">
          Your password has been updated successfully. Redirecting you to sign in…
        </p>
      </div>
    </AuthLayout>
  );

  return (
    <AuthLayout>
      <ResetStepper current={2} />
      <div className="flex justify-center mb-5">
        <div className="w-14 h-14 rounded-2xl bg-brand-blue/10 ring-1 ring-brand-blue/15 flex items-center justify-center">
          <Lock className="text-brand-blue" size={24} />
        </div>
      </div>

      <div className="text-center mb-6">
        <h1 className="text-[24px] sm:text-[26px] font-bold tracking-tight text-slate-900">
          Reset your password
        </h1>
        <p className="mt-2 text-sm text-slate-500 leading-relaxed">
          Create a new password for your Temveris account.
        </p>
        {email && (
          <p className="mt-1.5 text-xs text-slate-400">
            for <span className="font-semibold text-slate-600">{email}</span>
          </p>
        )}
      </div>

      <form onSubmit={handleSubmit} className="space-y-4 stagger">
        <div>
          <label
            htmlFor="zt-new-pw"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            New password
          </label>
          <div className="relative">
            <input
              id="zt-new-pw"
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
            htmlFor="zt-confirm-pw"
            className="block text-[13px] font-semibold text-slate-700 mb-1.5"
          >
            Confirm new password
          </label>
          <input
            id="zt-confirm-pw"
            type={showPw ? "text" : "password"}
            placeholder="Re-enter your password"
            value={confirm}
            onChange={e => setConfirm(e.target.value)}
            className={`w-full bg-slate-50/70 border rounded-xl px-4 py-3 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:bg-white ${
              confirm.length > 0
                ? passwordsMatch ? "border-brand-green" : "border-red-400"
                : "border-slate-200"
            }`}
            autoComplete="new-password"
            required
          />
          {confirm.length > 0 && !passwordsMatch && (
            <p className="text-xs text-red-500 mt-1.5 ml-1">Passwords do not match</p>
          )}
        </div>

        <button
          type="submit"
          disabled={loading || !allRulesMet || !passwordsMatch}
          className="btn-hover w-full text-white py-3 rounded-xl text-[15px] font-semibold flex items-center justify-center gap-2 disabled:opacity-50 disabled:cursor-not-allowed shadow-brandSoft hover:shadow-brand mt-1"
          style={{ background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)" }}
        >
          {loading ? (<><span className="loader" />Updating…</>) : "Update password"}
        </button>

        <div className="text-center pt-1">
          <Link
            to="/"
            className="inline-flex items-center gap-1 text-sm font-semibold text-brand-blue hover:text-brand-green-dark transition-colors"
          >
            <ChevronLeft size={14} /> Back to sign in
          </Link>
        </div>
      </form>
    </AuthLayout>
  );
}

export default ResetPassword;
