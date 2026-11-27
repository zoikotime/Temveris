import React, { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import API, { saveErrorMessage } from "../api/axios";
import AdminSidebar from "../components/AdminSidebar";
import TopNavbar from "../components/TopNavbar";
import { useLanguage } from "../context/LanguageContext";
import { useAuth } from "../context/AuthContext";

// ─── palette (matches AdminSidebar + AdminDashboard light theme) ─────────────
const C = {
  bg:          "#F5F7FA",
  surface:     "#FFFFFF",
  border:      "#E4E7EC",
  borderLight: "#F2F4F7",
  text:        "#101828",
  textSub:     "#475467",
  textLight:   "#98A2B3",
  brand:       "#11496A",
  brandDark:   "#0E3B57",
  brandBg:     "#E7F0F6",
  brandAccent: "#44B97B",
  danger:      "#B42318",
  r:           10,
  rSm:         6,
};

const initials = (name) =>
  (name || "").split(" ").map((w) => w[0]).join("").toUpperCase().slice(0, 2) || "U";

// Downscale an image file to a compact 256×256 JPEG data URL so the payload
// stays well under the backend's 2 MB cap even when users pick huge photos.
function fileToCompactDataURL(file, maxSide = 256, quality = 0.85) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const img = new Image();
      img.onload = () => {
        const scale = Math.min(1, maxSide / Math.max(img.width, img.height));
        const w = Math.max(1, Math.round(img.width  * scale));
        const h = Math.max(1, Math.round(img.height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w; canvas.height = h;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(img, 0, 0, w, h);
        resolve(canvas.toDataURL("image/jpeg", quality));
      };
      img.onerror = reject;
      img.src = reader.result;
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

export default function AccountSettings() {
  const navigate = useNavigate();
  const { T } = useLanguage();
  const { user: authUser, refreshUser, updateUserLocal } = useAuth();
  const fileRef = useRef(null);

  const role = (authUser?.role || localStorage.getItem("role") || "").toLowerCase();
  const isAdmin = role === "admin" || role === "hr";

  const [loading, setLoading] = useState(true);
  const [saving,  setSaving]  = useState(false);
  const [name,    setName]    = useState("");
  const [email,   setEmail]   = useState("");
  const [picture, setPicture] = useState(null); // data URL or null

  // ── Load current profile on mount ──
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const { data } = await API.get("/auth/me");
        if (cancelled) return;
        setName(data.name || "");
        setEmail(data.email || "");
        setPicture(data.profile_picture || null);
      } catch (err) {
        toast.error(T.couldntLoadProfile || "Couldn't load your profile");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  const handlePickFile = () => fileRef.current?.click();

  const handleFileChange = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // let users re-pick the same file
    if (!file) return;
    if (!/^image\/(png|jpe?g)$/i.test(file.type)) {
      toast.error(T.pleaseUploadPngJpg || "Please upload a PNG or JPG image");
      return;
    }
    try {
      const dataUrl = await fileToCompactDataURL(file);
      setPicture(dataUrl);
    } catch {
      toast.error(T.couldntReadImage || "Couldn't read that image — try another file");
    }
  };

  const handleRemovePicture = () => setPicture(null);

  const handleSave = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      toast.error(T.nameCantBeEmpty || "Name can't be empty");
      return;
    }
    setSaving(true);
    try {
      const { data } = await API.patch("/auth/me", {
        name:            trimmed,
        profile_picture: picture, // null clears it
      });
      // Update AuthContext so the topbar avatar/name flips immediately
      // across the entire app, then re-fetch to reconcile derived fields.
      updateUserLocal({
        name: data.name || trimmed,
        profile_picture: data.profile_picture ?? null,
      });
      try { await refreshUser(); } catch {}
      toast.success(T.personalInfoSaved || "Personal info saved");
    } catch (err) {
      toast.error(saveErrorMessage(err, T.couldntSave || "Couldn't save — please try again"));
    } finally {
      setSaving(false);
    }
  };

  // ── Page body (shared by admin and employee layouts) ──
  const body = (
    <main style={{ flex: 1, background: C.bg, overflowY: "auto" }}>
      <div style={{ maxWidth: 820, margin: "0 auto", padding: "40px 32px" }}>
        <h1 style={{ fontSize: 26, fontWeight: 800, color: C.text, marginBottom: 24 }}>
          {T.personalInfo || "Personal Info"}
        </h1>

        {loading ? (
          <div style={{ padding: 40, textAlign: "center", color: C.textSub, fontSize: 14 }}>
            {T.loadingEllipsis || "Loading…"}
          </div>
        ) : (
          <div style={{ background: C.surface, borderRadius: C.r, border: `1px solid ${C.border}`, padding: 32, boxShadow: "0 1px 2px rgba(16,24,40,0.04)" }}>
            {/* Full Name */}
            <div style={{ marginBottom: 20 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 6 }}>
                {T.fullName || "Full Name"}
              </label>
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={T.yourFullName || "Your full name"}
                style={{
                  width: "100%", padding: "11px 14px", borderRadius: C.rSm,
                  border: `1px solid ${C.border}`, background: C.bg,
                  fontSize: 14, color: C.text, fontFamily: "inherit", outline: "none",
                  boxSizing: "border-box",
                }}
                onFocus={(e) => { e.target.style.borderColor = C.brand; e.target.style.background = "#fff"; }}
                onBlur={(e)  => { e.target.style.borderColor = C.border; e.target.style.background = C.bg; }}
              />
            </div>

            {/* Email (read-only — identity key) */}
            <div style={{ marginBottom: 20 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 6 }}>
                {T.emailAddress || "Email Address"}
              </label>
              <input
                type="email"
                value={email}
                disabled
                style={{
                  width: "100%", padding: "11px 14px", borderRadius: C.rSm,
                  border: `1px solid ${C.border}`, background: C.borderLight,
                  fontSize: 14, color: C.textSub, fontFamily: "inherit", outline: "none",
                  cursor: "not-allowed", boxSizing: "border-box",
                }}
              />
              <p style={{ fontSize: 11, color: C.textLight, marginTop: 4 }}>
                {T.emailIdentityHelp || "Email is your login identity and can't be changed here."}
              </p>
            </div>

            {/* Profile Picture */}
            <div style={{ marginBottom: 24 }}>
              <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: C.text, marginBottom: 8 }}>
                {T.profilePicture || "Profile Picture"}
              </label>
              <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
                <div style={{
                  width: 72, height: 72, borderRadius: "50%",
                  background: picture ? `url(${picture}) center/cover` : `linear-gradient(135deg,${C.brand},${C.brandDark})`,
                  display: "flex", alignItems: "center", justifyContent: "center",
                  color: "#fff", fontWeight: 800, fontSize: 22,
                  border: `2px solid ${C.border}`, flexShrink: 0,
                }}>
                  {!picture && initials(name)}
                </div>
                <div style={{ flex: 1 }}>
                  <button
                    type="button"
                    onClick={handlePickFile}
                    style={{
                      background: "none", border: "none", padding: 0,
                      color: C.brand, fontWeight: 700, fontSize: 14,
                      cursor: "pointer", fontFamily: "inherit",
                    }}>
                    {T.uploadYourPhoto || "Upload your photo"}
                  </button>
                  <p style={{ fontSize: 12, color: C.textSub, marginTop: 2 }}>
                    {T.profilePictureFormatHelp || "Profile picture should be in the standard format png or jpg."}
                  </p>
                  {picture && (
                    <button
                      type="button"
                      onClick={handleRemovePicture}
                      style={{
                        marginTop: 6, background: "none", border: "none", padding: 0,
                        color: C.danger, fontWeight: 600, fontSize: 12,
                        cursor: "pointer", fontFamily: "inherit",
                      }}>
                      {T.removePhoto || "Remove photo"}
                    </button>
                  )}
                </div>
              </div>
              <input
                ref={fileRef}
                type="file"
                accept="image/png,image/jpeg"
                onChange={handleFileChange}
                style={{ display: "none" }}
              />
            </div>

            <button
              onClick={handleSave}
              disabled={saving}
              style={{
                padding: "11px 22px", borderRadius: C.rSm, border: "none",
                background: saving ? C.textLight : C.brand, color: "#fff",
                fontWeight: 700, fontSize: 14, cursor: saving ? "default" : "pointer",
                fontFamily: "inherit", boxShadow: "0 1px 2px rgba(17,73,106,0.25)",
              }}>
              {saving ? (T.savingDots || "Saving…") : (T.savePersonalInfo || "Save Personal Info")}
            </button>
          </div>
        )}
      </div>
    </main>
  );

  // ── Admin / HR: persistent admin chrome (sidebar + top navbar) ──
  if (isAdmin) {
    return (
      <div style={{ display: "flex", height: "100vh", overflow: "hidden", background: C.bg, fontFamily: "Inter, -apple-system, sans-serif" }}>
        <AdminSidebar activeKey="accountsettings" />
        <div style={{ flex: 1, display: "flex", flexDirection: "column", minWidth: 0, overflow: "hidden" }}>
          <TopNavbar
            title={T.accountSettings || "Account Settings"}
            onBack={() => navigate(-1)}
          />
          {body}
        </div>
      </div>
    );
  }

  // ── Employee: shared TopNavbar (with profile dropdown + theme toggle) ──
  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column", background: C.bg, fontFamily: "Inter, -apple-system, sans-serif" }}>
      <TopNavbar
        title={T.accountSettings || "Account Settings"}
        onBack={() => navigate(-1)}
      />
      {body}
    </div>
  );
}
