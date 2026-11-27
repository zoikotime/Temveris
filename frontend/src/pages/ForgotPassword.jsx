/**
 * ForgotPassword — OTP flow, step 1 of 3.
 *
 * Flow:
 *   1. User enters their work email here → POST /auth/forgot-password
 *   2. Backend checks the account exists, then emails a 4-digit OTP from
 *      support@temveris.example (stored only as a hash) and returns
 *      { account_exists, email_sent, message }.
 *   3. On success we navigate to /verify-otp, carrying the email in router
 *      state (never the URL) so it can't leak via referrer or history.
 *   4. If no account matches, we stay here and show "No account found…".
 */
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Mail } from "lucide-react";
import { forgotPassword } from "../api/authApi";
import toast from "react-hot-toast";
import ResetStepper from "../components/auth/ResetStepper";

const isValidEmail = (s) =>
  /^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$/.test((s || "").trim());

function ForgotPassword() {
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [touched, setTouched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [serverError, setServerError] = useState(""); // e.g. "No account found…"

  const emailValid = isValidEmail(email);
  const showFormatError = touched && email.length > 0 && !emailValid;

  const requestOtp = async () => {
    if (!emailValid) {
      setTouched(true);
      toast.error("Enter a valid email address");
      return;
    }

    setLoading(true);
    setServerError("");

    try {
      const res = await forgotPassword({ email: email.trim() });
      const data = res?.data || {};

      // Account doesn't exist — stay on this step and tell the user.
      if (data.account_exists === false) {
        const msg = data.message || "No account found with this work email.";
        setServerError(msg);
        toast.error(msg);
        return;
      }

      if (data.throttled) {
        toast("A code was sent recently. Please check your inbox.", { icon: "⏳" });
        navigate("/verify-otp", { state: { email: email.trim() } });
        return;
      }

      toast.success(data.message || "OTP has been sent successfully to your work email.");
      navigate("/verify-otp", { state: { email: email.trim() } });
    } catch (err) {
      const isTimeout = err?.code === "ECONNABORTED" || /timeout/i.test(err?.message || "");
      const msg = isTimeout
        ? "The server is taking longer than expected. Please try again in a moment."
        : err?.response?.data?.detail || err?.message || "Network error — please try again";
      setServerError(msg);
      toast.error(msg);
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = (e) => {
    e.preventDefault();
    setTouched(true);
    requestOtp();
  };

  // The card, logo and tab bar come from AuthShell, which stays mounted across
  // the tabbed auth routes — this page renders only its own content.
  return (
    <>
      <ResetStepper current={0} />

      <div className="flex justify-center mb-5">
        <div className="w-14 h-14 rounded-2xl bg-brand-blue/10 ring-1 ring-brand-blue/15 flex items-center justify-center">
          <Mail className="text-brand-blue" size={24} />
        </div>
      </div>

      <div className="text-center mb-6">
        <h1 className="text-[24px] sm:text-[26px] font-bold tracking-tight text-slate-900">
          Forgot Password
        </h1>
        <p className="mt-2 text-sm text-slate-500 leading-relaxed">
          Enter your registered work email to receive a verification OTP.
        </p>
      </div>

      <form onSubmit={handleSubmit} className="space-y-4 stagger" noValidate>
        <div>
          <label
            htmlFor="zt-fp-email"
            className="block text-[13px] font-semibold text-brand-green-dark mb-1.5"
          >
            Work Email <span className="text-red-500">*</span>
          </label>
          <input
            id="zt-fp-email"
            type="email"
            placeholder="you@company.com"
            value={email}
            onChange={(e) => { setEmail(e.target.value); if (serverError) setServerError(""); }}
            onBlur={() => setTouched(true)}
            className={
              "w-full bg-white border rounded-xl px-4 py-3 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:ring-2 focus:ring-brand-green/15 " +
              (showFormatError || serverError ? "border-red-400" : "border-slate-200 focus:border-brand-green")
            }
            autoFocus
            autoComplete="email"
            required
          />
          {showFormatError && (
            <p className="text-xs text-red-500 mt-1.5 ml-1">Enter a valid email address</p>
          )}
          {!showFormatError && serverError && (
            <p className="text-xs text-red-500 mt-1.5 ml-1">{serverError}</p>
          )}
        </div>

        <button
          type="submit"
          disabled={loading || !emailValid}
          className="btn-hover w-full text-white py-3 rounded-xl text-[15px] font-semibold flex items-center justify-center gap-2 disabled:opacity-60 disabled:cursor-not-allowed mt-1 shadow-brandSoft hover:shadow-brand"
          style={{ background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)" }}
        >
          {loading ? (<><span className="loader" /> Sending…</>) : "Send OTP"}
        </button>

        <Link
          to="/"
          className="w-full block text-center py-2.5 rounded-xl border border-slate-200 bg-white text-sm font-semibold text-slate-700 hover:bg-slate-50 transition-colors"
        >
          Cancel
        </Link>
      </form>
    </>
  );
}

export default ForgotPassword;
