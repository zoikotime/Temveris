/**
 * VerifyOtp — OTP flow, step 2 of 3.
 *
 * Reached from /forgot-password via router state ({ email }). If the user
 * lands here directly (refresh, bookmark) we bounce them back to step 1 —
 * we don't carry the email in the URL so it can't leak via referrer or
 * browser history.
 *
 * UX notes:
 *  • Cell entry (autofill, auto-advance, Backspace rewind, paste) now comes
 *    from the shared components/auth/OtpInput. It used to live inline here,
 *    hardcoded to four digits; the activation flow needs six, and one code
 *    entry implementation is better than two that drift. Behaviour on this
 *    screen is unchanged — same four cells, same interactions, same styling
 *    via tone="blue".
 *  • 5-minute countdown drives both the "Code expires in m:ss" hint and
 *    the enable-state of the Resend button.
 *  • Verify is auto-submitted once all four boxes are filled — saves a tap.
 */
import { useEffect, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { ShieldCheck, RefreshCw, ChevronLeft } from "lucide-react";
import toast from "react-hot-toast";
import { forgotPassword, verifyOtp } from "../api/authApi";
import AuthLayout from "../components/auth/AuthLayout";
import ResetStepper from "../components/auth/ResetStepper";
import OtpInput from "../components/auth/OtpInput";

const OTP_LENGTH         = 4;
const OTP_TTL_SECONDS    = 5 * 60;   // mirror of backend _OTP_TTL_SECONDS
const RESEND_MIN_WAIT    = 30;       // soft floor so users don't spam Resend

const fmtMs = (s) => {
  const m = Math.floor(s / 60);
  const r = String(s % 60).padStart(2, "0");
  return `${m}:${r}`;
};

const SUPPORT_EMAIL = "support@temveris.example";

function VerifyOtp() {
  const navigate = useNavigate();
  const location = useLocation();
  const email = location.state?.email || "";

  const [otp, setOtp]         = useState("");
  const [verifying, setVerifying] = useState(false);
  const [resending, setResending] = useState(false);
  const [secondsLeft, setSecondsLeft] = useState(OTP_TTL_SECONDS);
  const [resendCooldown, setResendCooldown] = useState(RESEND_MIN_WAIT);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!email) navigate("/forgot-password", { replace: true });
  }, [email, navigate]);

  useEffect(() => {
    if (secondsLeft <= 0) return;
    const id = setInterval(() => setSecondsLeft(s => Math.max(0, s - 1)), 1000);
    return () => clearInterval(id);
  }, [secondsLeft]);

  useEffect(() => {
    if (resendCooldown <= 0) return;
    const id = setInterval(() => setResendCooldown(s => Math.max(0, s - 1)), 1000);
    return () => clearInterval(id);
  }, [resendCooldown]);

  const otpReady = otp.length === OTP_LENGTH && /^\d+$/.test(otp);
  const expired = secondsLeft <= 0;

  const submitOtp = async () => {
    if (!otpReady || verifying) return;
    if (expired) {
      setError("OTP has expired. Please request a new OTP.");
      return;
    }
    setVerifying(true);
    setError("");
    try {
      await verifyOtp({ email, otp });
      toast.success("Verified ✅");
      navigate("/reset-password", { state: { email, otpVerified: true }, replace: true });
    } catch (err) {
      const msg = err?.response?.data?.detail || err?.message || "Couldn't verify the code";
      setError(msg);
      toast.error(msg);
      setOtp("");
    } finally {
      setVerifying(false);
    }
  };

  useEffect(() => {
    if (otpReady && !verifying && !expired) {
      submitOtp();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [otpReady]);

  const handleResend = async () => {
    if (resendCooldown > 0 || resending) return;
    setResending(true);
    setError("");
    try {
      await forgotPassword({ email });
      toast.success("A new code has been sent 📩");
      setOtp("");
      setSecondsLeft(OTP_TTL_SECONDS);
      setResendCooldown(RESEND_MIN_WAIT);
    } catch (err) {
      const msg = err?.response?.data?.detail || err?.message || "Couldn't resend the code";
      toast.error(msg);
    } finally {
      setResending(false);
    }
  };

  return (
    <AuthLayout
      footer={
        <>
          Didn't get it? Check spam, or{" "}
          <a
            href={`mailto:${SUPPORT_EMAIL}`}
            className="font-semibold text-brand-blue hover:underline underline-offset-2"
          >
            contact support
          </a>
        </>
      }
    >
      <ResetStepper current={1} />

      <div className="flex justify-center mb-5">
        <div className="w-14 h-14 rounded-2xl bg-brand-blue/10 ring-1 ring-brand-blue/15 flex items-center justify-center">
          <ShieldCheck className="text-brand-blue" size={24} />
        </div>
      </div>

      <div className="text-center mb-6">
        <h1 className="text-[24px] sm:text-[26px] font-bold tracking-tight text-slate-900">
          Verify OTP
        </h1>
        <p className="mt-2 text-sm text-slate-500 leading-relaxed">
          We sent a 4-digit code to{" "}
          <span className="font-semibold text-slate-700 break-all">{email}</span>.
        </p>
      </div>

      <div className="space-y-5 stagger">
        <div className="flex justify-center">
          <div className="w-full max-w-[260px]">
            <OtpInput
              id="zt-reset-otp"
              value={otp}
              onChange={setOtp}
              onComplete={submitOtp}
              length={OTP_LENGTH}
              disabled={verifying || expired}
              invalid={Boolean(error)}
              tone="blue"
              label="One-time password"
            />
          </div>
        </div>

        {error && (
          <p className="text-center text-xs text-red-500 -mt-2">{error}</p>
        )}

        <div className="text-center text-xs">
          {expired ? (
            <span className="text-red-500 font-semibold">Code expired — request a new one.</span>
          ) : (
            <span className="text-slate-500">
              Code expires in{" "}
              <span className="font-semibold text-slate-800 tabular-nums">{fmtMs(secondsLeft)}</span>
            </span>
          )}
        </div>

        <button
          onClick={submitOtp}
          disabled={!otpReady || verifying || expired}
          className="btn-hover w-full text-white py-3 rounded-xl text-[15px] font-semibold flex items-center justify-center gap-2 shadow-brandSoft hover:shadow-brand disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ background: "linear-gradient(135deg,#11496A 0%,#44B97B 100%)" }}
        >
          {verifying ? (<><span className="loader" /> Verifying…</>) : "Verify code"}
        </button>

        <button
          onClick={handleResend}
          disabled={resending || resendCooldown > 0}
          className="w-full py-2.5 rounded-xl border border-slate-200 bg-white text-sm font-semibold text-slate-700 hover:bg-slate-50 disabled:opacity-60 disabled:cursor-not-allowed flex items-center justify-center gap-2 transition-colors"
        >
          <RefreshCw size={14} />
          {resending
            ? "Resending…"
            : resendCooldown > 0
              ? `Resend in ${resendCooldown}s`
              : "Resend code"}
        </button>

        <div className="text-center pt-1">
          <Link
            to="/forgot-password"
            className="inline-flex items-center gap-1 text-sm font-semibold text-brand-blue hover:text-brand-green-dark transition-colors"
          >
            <ChevronLeft size={14} /> Use a different email
          </Link>
        </div>
      </div>
    </AuthLayout>
  );
}

export default VerifyOtp;
