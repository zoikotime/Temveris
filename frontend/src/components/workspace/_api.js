// Workspace Settings shared API helper.
// All Workspace Settings tabs (Roles, Policies, Billing summary, Security,
// Integrations, Audit) call /api/workspace/* — the only place that lives.
import { API_BASE } from "../../config";

const token = () => localStorage.getItem("zt_token") || localStorage.getItem("token");

export async function wsGet(path) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { Authorization: `Bearer ${token()}` },
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

export async function wsSend(method, path, body) {
  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers: { Authorization: `Bearer ${token()}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = `${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch (_e) {}
    throw new Error(detail);
  }
  return res.json();
}
