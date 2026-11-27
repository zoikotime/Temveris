import { useState, useEffect } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Eye, EyeOff } from "lucide-react";
import { loginUser } from "../api/authApi";
import toast from "react-hot-toast";
import { useLanguage } from "../context/LanguageContext";
import { useAuth } from "../context/AuthContext";
import { takeLogoutNotice } from "../api/authStorage";

function Login() {
  const { T } = useLanguage();
  const { login, isAuthenticated, user, loading: authLoading } = useAuth();

  // Read persisted "Remember this device" preference once so an explicit
  // uncheck survives reloads. Defaults to true on first visit because most
  // users — and every Electron install (the app lives on a personal machine,
  // not a shared kiosk) — want auto-login.
  const REMEMBER_PREF_KEY = "zt_remember_pref";
  const readRememberPref = () => {
    try { return localStorage.getItem(REMEMBER_PREF_KEY) !== "0"; }
    catch { return true; }
  };

  const [showPassword, setShowPassword] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [rememberMe, setRememberMe] = useState(readRememberPref);
  const [loading, setLoading] = useState(false);

  const navigate = useNavigate();

  useEffect(() => {
    // Electron: pull the remembered email + password out of the encrypted
    // safeStorage file (DPAPI / Keychain / libsecret) and prefill both inputs.
    // This is the Electron equivalent of the browser's "Save password?" prompt
    // — the bundled Chromium in Electron doesn't ship the password manager UI,
    // so we persist it ourselves through the main process.
    if (window.electronAuth?.getCredentials) {
      const c = window.electronAuth.getCredentials();
      if (c?.email)    setEmail(c.email);
      if (c?.password) setPassword(c.password);
      if (c?.email || c?.password) return; // skip the browser-side path below
    }

    try {
      const savedEmail = localStorage.getItem("zt_remembered_email");
      if (savedEmail) setEmail(savedEmail);
    } catch { /* private-mode browsers throw on storage access — ignore */ }
  }, []);

  // A session that ended for a stated reason — today, an organization the
  // super admin suspended mid-session — leaves a one-shot notice behind. Show
  // it once so the sign-out reads as an explanation rather than a glitch.
  useEffect(() => {
    const notice = takeLogoutNotice();
    if (notice) toast(notice, { icon: "⚠️", duration: 8000 });
  }, []);

  const handleRememberChange = (checked) => {
    setRememberMe(checked);
    try { localStorage.setItem(REMEMBER_PREF_KEY, checked ? "1" : "0"); }
    catch { /* private-mode browsers throw on storage access — ignore */ }
  };

  // Skip-login auto-redirect — see AuthContext + /auth/me rehydration.
  useEffect(() => {
    if (authLoading) return;
    if (!isAuthenticated || !user) return;
    const role = (user.role || "").toLowerCase();
    if (window.isDesktopApp === true) {
      navigate("/electron", { replace: true });
    } else if (role === "super_admin") {
      navigate("/super-admin", { replace: true });
    } else if (["admin", "hr"].includes(role)) {
      navigate("/admin", { replace: true });
    } else {
      navigate("/dashboard", { replace: true });
    }
  }, [authLoading, isAuthenticated, user, navigate]);

  const handleLogin = async (e) => {
    e.preventDefault();
    try {
      setLoading(true);
      // `remember` drives the backend refresh-token lifetime (30 days vs a
      // transient session) and the storage tier on the client.
      const res = await loginUser({ email, password, remember: rememberMe });
      const data = res?.data;

      if (data?.access_token) {
        toast.success("Login successful 🎉");

        const u = data.user || {};

        // Trigger the browser's native "Save password?" prompt. Chromium's
        // heuristic detector is unreliable for SPAs that login over XHR
        // (axios) instead of a real form POST + top-level navigation —
        // Vivaldi/Edge/Chrome often skip the prompt. The Credential
        // Management API tells the browser explicitly, which is what
        // Google's own login pages do.
        try {
          if (window.PasswordCredential) {
            const cred = new window.PasswordCredential({
              id:       email,
              password: password,
              name:     u.name || email,
            });
            navigator.credentials?.store(cred);
          }
        } catch { /* unsupported browser — fall back to heuristic detector */ }

        await login(
          data.access_token,
          {
            id:              u.id,
            name:            u.name,
            email:           u.email,
            role:            u.role,
            profile_picture: u.profile_picture || null,
            organization_id: u.organization_id ?? null,
          },
          { remember: rememberMe },
        );

        if (rememberMe) {
          localStorage.setItem("zt_remembered_email", email);
        } else {
          localStorage.removeItem("zt_remembered_email");
        }

        if (window.electronAuth?.saveSession) {
          window.electronAuth.saveSession({
            token: data.access_token,
            // Long-lived rotating refresh token — Electron's main process seals
            // this with OS-level safeStorage so the desktop app stays logged in
            // across restarts and can silently renew the short access token.
            refresh_token: data.refresh_token || null,
            name: u.name,
            email: u.email,
            role: u.role,
            user_id: String(u.id),
          });
        }

        // Electron-native "Save password" — mirrors the Vivaldi/Chrome prompt
        // the bundled Chromium in Electron doesn't provide. The main process
        // encrypts with OS-level safeStorage before writing to disk.
        if (window.electronAuth?.saveCredentials && window.electronAuth?.clearCredentials) {
          if (rememberMe) {
            window.electronAuth.saveCredentials({ email, password });
          } else {
            window.electronAuth.clearCredentials();
          }
        }

        if (window.isDesktopApp === true) {
          navigate("/electron");
        } else if ((u.role || "").toLowerCase() === "super_admin") {
          navigate("/super-admin");
        } else if (["admin", "hr"].includes((u.role || "").toLowerCase())) {
          navigate("/admin");
        } else {
          navigate("/dashboard");
        }
      } else {
        toast.error("Login failed ❌");
      }
    } catch (error) {
      // `detail` is a bare string on most auth errors, but a {code, message}
      // object on the structured ones (organization suspended, seat limits).
      // Reading `.message` off the object keeps those from rendering as
      // "[object Object]" in the toast.
      const detail = error.response?.data?.detail;
      const backendMessage =
        typeof detail === "string" ? detail : detail?.message;
      toast.error(backendMessage || "Invalid credentials ❌");
    } finally {
      setLoading(false);
    }
  };

  if (authLoading || (isAuthenticated && user)) {
    return (
      <div className="flex flex-col items-center justify-center py-16 gap-3">
        <span className="loader" />
        <p className="text-[13px] text-slate-500">Welcoming you back…</p>
      </div>
    );
  }

  // The card, logo and tab bar come from AuthShell, which stays mounted across
  // the tabbed auth routes — this page renders only its own content.
  return (
    <>
      <h1 className="text-[26px] font-bold tracking-tight text-slate-900">
        Welcome back!
      </h1>
      <p className="mt-1.5 mb-5 text-[14px] text-slate-500">
        {T.loginSubtitle || "Enter your credentials to access your account."}
      </p>

      <form onSubmit={handleLogin} className="space-y-4" autoComplete="on" noValidate>
        <div>
          <label
            htmlFor="zt-email"
            className="block text-[13px] font-semibold text-brand-green-dark mb-1.5"
          >
            Username or Email Address
          </label>
          <input
            id="zt-email"
            type="text"
            name="email"
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full bg-white border border-slate-200 rounded-xl px-4 py-3 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:border-brand-green focus:ring-2 focus:ring-brand-green/15"
            required
          />
        </div>

        <div>
          <label
            htmlFor="zt-password"
            className="block text-[13px] font-semibold text-brand-green-dark mb-1.5"
          >
            Password
          </label>
          <div className="relative">
            <input
              id="zt-password"
              type={showPassword ? "text" : "password"}
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full bg-white border border-slate-200 rounded-xl px-4 pr-11 py-3 text-[15px] text-slate-800 placeholder-slate-400 input-glow focus:outline-none focus:border-brand-green focus:ring-2 focus:ring-brand-green/15"
              required
            />
            <button
              type="button"
              onClick={() => setShowPassword((v) => !v)}
              aria-label={showPassword ? "Hide password" : "Show password"}
              className="absolute right-3 top-1/2 -translate-y-1/2 eye-btn text-slate-400 p-1 hover:text-slate-600"
            >
              {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
            </button>
          </div>
          <div className="flex justify-end mt-1.5">
            <Link
              to="/forgot-password"
              className="text-[13px] font-semibold text-brand-green-dark hover:underline underline-offset-2"
            >
              Forgot Password?
            </Link>
          </div>
        </div>

        <label className="flex items-center gap-2.5 text-[14px] text-slate-600 cursor-pointer select-none pt-1">
          <input
            type="checkbox"
            checked={rememberMe}
            onChange={(e) => handleRememberChange(e.target.checked)}
            className="w-4 h-4 rounded border-slate-300 accent-brand-green cursor-pointer"
          />
          Remember for 30 days
        </label>

        <button
          type="submit"
          disabled={loading}
          className="w-full text-white py-3 rounded-xl text-[15px] font-semibold flex items-center justify-center gap-2 disabled:opacity-70 disabled:cursor-not-allowed mt-1 transition-colors"
          style={{ background: loading ? "#2F8F5C" : "#1FB389" }}
        >
          {loading ? (<><span className="loader" /> Signing in…</>) : "Log In"}
        </button>
      </form>

      <div className="mt-5 text-center text-[14px]">
        <span className="text-slate-500">New to Temveris? </span>
        <Link
          to="/start"
          className="font-semibold text-brand-green-dark hover:underline underline-offset-2"
        >
          Create Account
        </Link>
      </div>

    </>
  );
}

export default Login;
