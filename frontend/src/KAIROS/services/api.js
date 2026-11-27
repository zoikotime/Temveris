import axios from "axios";

// Kairos is now an in-process FastAPI module (backend/app/kairos), mounted
// at /api/v1/kairos. The standalone Node service (KAIROS-BE) is gone, and
// the separate Kairos JWT bootstrap with it — every request authenticates
// with the main Temveris token.
//
// Base-URL resolution (in priority order):
//   1. VITE_CHATBOT_API_URL — explicit override if Kairos is on its own host.
//   2. VITE_API_URL + "/api/v1/kairos" — the main app's API host (Cloud Run,
//      etc.). Used when the frontend ships separately from the backend
//      (e.g. Vercel/Netlify hosting the SPA while Cloud Run serves the API).
//      WITHOUT this branch, Kairos requests resolved to the frontend's own
//      host in production (404), even though every other API call worked,
//      because the rest of the app reads VITE_API_URL via src/config.js.
//   3. "/api/v1/kairos" — relative path. Works for the bundled Docker deploy
//      where the FastAPI app serves the React build under the same origin,
//      and for local dev because Vite's proxy routes /api → 127.0.0.1:8000.
function resolveBaseUrl() {
  const explicit = (import.meta.env.VITE_CHATBOT_API_URL || "").trim();
  if (explicit) return explicit.replace(/\/+$/, "");
  const mainApi = (import.meta.env.VITE_API_URL || "").trim();
  if (mainApi) return mainApi.replace(/\/+$/, "") + "/api/v1/kairos";
  return "/api/v1/kairos";
}

const RESOLVED_BASE_URL = resolveBaseUrl();

// Emit once at module load so the resolved host shows up in browser DevTools
// on first paint — invaluable when triaging "works locally, silent in prod".
if (typeof console !== "undefined") {
  // eslint-disable-next-line no-console
  console.info("[Kairos] API base:", RESOLVED_BASE_URL);
}

const api = axios.create({
  baseURL: RESOLVED_BASE_URL,
  timeout: 30000,
});

function readMainAppToken() {
  return (
    localStorage.getItem("zt_token") ||
    localStorage.getItem("token") ||
    ""
  );
}

api.interceptors.request.use((config) => {
  const token = readMainAppToken();
  if (token) {
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});

// CHAT
export async function sendMessage(payload) {
  const { data } = await api.post("/chat", payload);
  return data;
}

// SESSIONS
// Used by the mail panel when the user opens it before sending any chat
// message — the backend's /mail/send requires a real conversation, so we
// mint one on-demand here to keep the support-mail flow first-class.
export async function createSession() {
  const { data } = await api.post("/chat/sessions");
  return data;
}

// Normalize an axios failure into a {status, code, message} shape the UI
// can branch on. Without this the caller only sees an opaque AxiosError
// and falls back to "couldn't connect" for every failure mode.
export function describeApiError(err) {
  if (!err) return { status: 0, code: "UNKNOWN", message: "Unknown error" };
  if (err.code === "ECONNABORTED") {
    return { status: 0, code: "TIMEOUT", message: "Request timed out" };
  }
  const status = err.response?.status ?? 0;
  const detail = err.response?.data?.detail;
  const message =
    (typeof detail === "string" && detail) ||
    detail?.message ||
    err.message ||
    "Request failed";
  const code =
    detail?.code ||
    (status === 401 ? "UNAUTHORIZED" :
     status === 403 ? "FORBIDDEN" :
     status === 429 ? "RATE_LIMITED" :
     status >= 500 ? "SERVER_ERROR" :
     status === 0 ? "NETWORK" : "HTTP_ERROR");
  return { status, code, message };
}

export async function fetchHistory(sessionId) {
  const { data } = await api.get(`/chat/history/${sessionId}`);
  return data;
}

export async function fetchUserSessions() {
  const { data } = await api.get("/chat/sessions");

  return {
    ...data,
    sessions: (data.sessions || []).filter((s) => s.messageCount > 0),
  };
}

// SESSION MANAGEMENT
export async function endChatSession(sessionId) {
  const { data } = await api.patch(`/chat/sessions/${sessionId}/end`);
  return data;
}

export async function deleteChatSession(sessionId) {
  const { data } = await api.delete(`/chat/sessions/${sessionId}`);
  return data;
}

// MAIL
// Backend keys rate limit by user.email — always include it.
// Backend builds the HTML itself from sessionId, so we only send metadata.
export async function sendMail({ sessionId, user, to, subject, body }) {
  const { data } = await api.post(
    "/mail/send",
    {
      sessionId,
      user,
      to,
      subject,
      body,
      email: user?.email, // explicit top-level key for rate limiter middleware
    },
    {
      timeout: 30000,
    },
  );
  return data;
}

export async function fetchMailStatus() {
  const { data } = await api.get("/mail/status");
  return data;
}

export default api;
