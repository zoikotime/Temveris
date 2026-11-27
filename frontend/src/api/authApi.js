import API from "./axios";

// Auth endpoints sometimes hit a cold-start spike in production (Cloud Run
// container wake-up, DB connection pool warming up). The global axios
// timeout is 15 s which is tight for that — give each auth call a 45 s
// override so the user never sees a fake "timeout" error during normal
// transient slowness. Real failures still surface fast via the normal
// error path.
const AUTH_TIMEOUT_MS = 45_000;

export const registerUser = (data) => {
  return API.post("/auth/register", data, { timeout: AUTH_TIMEOUT_MS });
};

export const loginUser = (data) => {
  return API.post("/auth/login-json", data, { timeout: AUTH_TIMEOUT_MS });
};

// Step 1 — server saves the OTP synchronously then queues the email send
// as a FastAPI BackgroundTask. The response itself is fast (<100 ms in
// normal conditions); the longer timeout is just a cold-start safety net.
export const forgotPassword = (data) => {
  return API.post("/auth/forgot-password", data, { timeout: AUTH_TIMEOUT_MS });
};

// Step 2 — exchanges a 4-digit OTP for a verified-status row server-side.
// Caller passes { email, otp }; server responds with { success, message }
// or a 4xx with `detail` on bad / expired / locked codes.
export const verifyOtp = (data) => {
  return API.post("/auth/verify-otp", data, { timeout: AUTH_TIMEOUT_MS });
};

// Step 3 — sets the new password. Server requires a verified OTP row for
// the email to exist within the grace window or it 400s.
export const resetPassword = (data) => {
  return API.post("/auth/reset-password", data, { timeout: AUTH_TIMEOUT_MS });
};