import { useParams, useNavigate } from "react-router-dom";
import { useState, useEffect } from "react";

import { API_BASE as API } from "../config";

export default function InviteSignup() {
  const { token } = useParams();
  const navigate   = useNavigate();

  const [status,   setStatus]   = useState("loading"); // "loading"|"valid"|"invalid"
  const [email,    setEmail]    = useState("");
  const [name,     setName]     = useState("");
  const [password, setPassword] = useState("");
  const [confirm,  setConfirm]  = useState("");
  const [showPw,   setShowPw]   = useState(false);
  const [error,    setError]    = useState("");
  const [loading,  setLoading]  = useState(false);

  useEffect(() => {
    fetch(`${API}/auth/invite/verify/${token}`)
      .then(r => r.json())
      .then(d => {
        if (d.valid) {
          setEmail(d.email || "");
          setName(d.name  || "");
          setStatus("valid");
        } else {
          setStatus("invalid");
        }
      })
      .catch(() => setStatus("invalid"));
  }, [token]);

  const validate = () => {
    if (!name.trim())          return "Full name is required";
    if (password.length < 8)   return "Password must be at least 8 characters";
    if (!/[A-Z]/.test(password)) return "Password must include an uppercase letter";
    if (!/[0-9]/.test(password)) return "Password must include a number";
    if (!/[@$!%*?&]/.test(password)) return "Password must include a special character (@$!%*?&)";
    if (password !== confirm)  return "Passwords do not match";
    return null;
  };

  const handleSubmit = async () => {
    const err = validate();
    if (err) { setError(err); return; }
    setError("");
    setLoading(true);
    try {
      const res = await fetch(`${API}/auth/set-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, password, name: name.trim() }),
      });
      const data = await res.json();
      if (!res.ok) { setError(data.detail || "Failed to set password"); return; }

      // Auto-login after password set
      if (data.access_token) {
        localStorage.setItem("zt_token",  data.access_token);
        localStorage.setItem("token",     data.access_token);
        localStorage.setItem("name",      data.name  || name.trim());
        localStorage.setItem("email",     data.email || email);
        localStorage.setItem("role",      data.role  || "employee");
        localStorage.setItem("user_id",   String(data.user_id || ""));

        // Notify Electron main process immediately (starts screenshot tracking)
        if (window.electronAuth?.saveSession) {
          window.electronAuth.saveSession({
            token:   data.access_token,
            name:    data.name || name.trim(),
            email:   data.email || email,
            role:    data.role || "employee",
            user_id: String(data.user_id || ""),
          });
        }

        navigate("/dashboard");
      } else {
        navigate("/");
      }
    } catch {
      setError("Could not connect to server. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  const inp = {
    width: "100%", border: "1px solid #D0D5DD", borderRadius: 8,
    padding: "11px 14px", fontSize: 14, fontFamily: "inherit",
    outline: "none", boxSizing: "border-box", color: "#101828",
  };

  if (status === "loading") return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "system-ui,sans-serif", background: "#F5F7FA" }}>
      <p style={{ color: "#667085", fontSize: 15 }}>Verifying invite link…</p>
    </div>
  );

  if (status === "invalid") return (
    <div style={{ minHeight: "100vh", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "system-ui,sans-serif", background: "#F5F7FA" }}>
      <div style={{ textAlign: "center", padding: 40 }}>
        <div style={{ fontSize: 40, marginBottom: 16 }}>❌</div>
        <h2 style={{ color: "#B42318", marginBottom: 8, fontSize: 20 }}>Invalid or Expired Invite</h2>
        <p style={{ color: "#667085", fontSize: 14 }}>This invite link has expired or is invalid. Ask your admin to resend the invitation.</p>
      </div>
    </div>
  );

  return (
    <div style={{ minHeight: "100vh", background: "#F5F7FA", display: "flex", alignItems: "center", justifyContent: "center", fontFamily: "'DM Sans',system-ui,sans-serif" }}>
      <div style={{ width: 460, background: "#fff", borderRadius: 16, boxShadow: "0 8px 40px rgba(0,0,0,0.10)", padding: "44px 40px", border: "1px solid #E4E7EC" }}>

        {/* Header */}
        <div style={{ textAlign: "center", marginBottom: 32 }}>
          <div style={{ fontSize: 24, fontWeight: 800, letterSpacing: -0.5, marginBottom: 16 }}>
            <span style={{ color: "#11496A" }}>Temveris</span>
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 700, color: "#101828", margin: "0 0 6px" }}>Set Up Your Account</h1>
          <p style={{ fontSize: 14, color: "#667085", margin: 0 }}>You've been invited to join Temveris</p>
        </div>

        {/* Work Email (read-only) */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: "#344054", display: "block", marginBottom: 5 }}>Work Email</label>
          <input type="email" value={email} readOnly
            style={{ ...inp, background: "#F9FAFB", color: "#667085", cursor: "not-allowed" }} />
        </div>

        {/* Full Name */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: "#344054", display: "block", marginBottom: 5 }}>
            Full Name <span style={{ color: "#DC2626" }}>*</span>
          </label>
          <input type="text" value={name} onChange={e => setName(e.target.value)}
            placeholder="Enter your full name" style={inp} />
        </div>

        {/* Password */}
        <div style={{ marginBottom: 16 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: "#344054", display: "block", marginBottom: 5 }}>
            Create Password <span style={{ color: "#DC2626" }}>*</span>
          </label>
          <div style={{ position: "relative" }}>
            <input type={showPw ? "text" : "password"} value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="Min 8 chars, uppercase, number, special (@$!%*?&)"
              style={{ ...inp, paddingRight: 44 }} />
            <button type="button" onClick={() => setShowPw(v => !v)}
              style={{ position: "absolute", right: 12, top: "50%", transform: "translateY(-50%)", background: "none", border: "none", cursor: "pointer", color: "#667085", fontSize: 12, fontFamily: "inherit" }}>
              {showPw ? "Hide" : "Show"}
            </button>
          </div>
        </div>

        {/* Confirm Password */}
        <div style={{ marginBottom: 24 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: "#344054", display: "block", marginBottom: 5 }}>
            Confirm Password <span style={{ color: "#DC2626" }}>*</span>
          </label>
          <input type="password" value={confirm} onChange={e => setConfirm(e.target.value)}
            placeholder="Re-enter your password" style={inp} />
        </div>

        {/* Error */}
        {error && (
          <div style={{ background: "#FEF2F2", border: "1px solid #FECDCA", borderRadius: 8, padding: "10px 14px", fontSize: 13, color: "#B42318", marginBottom: 16 }}>
            {error}
          </div>
        )}

        {/* Submit */}
        <button onClick={handleSubmit} disabled={loading}
          style={{ width: "100%", padding: "13px", borderRadius: 8, border: "none", background: loading ? "#7AA1B7" : "linear-gradient(135deg,#11496A 0%,#44B97B 100%)", color: "#fff", fontWeight: 700, fontSize: 15, cursor: loading ? "not-allowed" : "pointer", fontFamily: "inherit", transition: "background .2s, box-shadow .2s, transform .2s", boxShadow: loading ? "none" : "0 4px 14px rgba(17,73,106,0.30)" }}>
          {loading ? "Activating…" : "Activate Account & Sign In"}
        </button>

        <p style={{ textAlign: "center", fontSize: 12, color: "#98A2B3", marginTop: 16, lineHeight: 1.6 }}>
          Password must include: uppercase, lowercase, number & special character
        </p>
      </div>
    </div>
  );
}
