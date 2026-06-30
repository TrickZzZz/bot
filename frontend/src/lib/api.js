const API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000"

const TOKEN_KEY = "am_access_token"

export function getToken() {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

async function request(path, { method = "GET", body, auth = true } = {}) {
  const headers = { "Content-Type": "application/json" }
  if (auth) {
    const token = getToken()
    if (token) headers["Authorization"] = `Bearer ${token}`
  }

  const res = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  })

  if (res.status === 401) {
    clearToken()
    throw new Error("Session expired. Please log in again.")
  }

  if (!res.ok) {
    let detail = `Request failed (${res.status})`
    try {
      const data = await res.json()
      if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)
    } catch {
      // non-JSON error body, keep default
    }
    throw new Error(detail)
  }

  if (res.status === 204) return null
  return res.json()
}

export const api = {
  login: (username, password) =>
    request("/auth/login", { method: "POST", body: { username, password }, auth: false }),
  register: (username, password) =>
    request("/auth/register", { method: "POST", body: { username, password }, auth: false }),
  me: () => request("/auth/me"),

  listAccounts: (search = "") =>
    request(`/accounts${search ? `?search=${encodeURIComponent(search)}` : ""}`),
  getAccount: (id) => request(`/accounts/${id}`),
  createAccount: (data) => request("/accounts", { method: "POST", body: data }),
  updateAccount: (id, data) => request(`/accounts/${id}`, { method: "PUT", body: data }),
  deleteAccount: (id) => request(`/accounts/${id}`, { method: "DELETE" }),
  bulkImport: (accounts) =>
    request("/accounts/bulk-import", { method: "POST", body: { accounts } }),
}
