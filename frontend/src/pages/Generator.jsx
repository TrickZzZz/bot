import * as React from "react"

const LEVEL_COLOR = {
  ok: "#3fb950", error: "#f85149", warn: "#d29922",
  muted: "#484f58", info: "#8b949e", __done__: "#484f58",
}
const KEY_COLOR = {
  ready: "#3fb950", busy: "#d29922", ok: "#3fb950",
  warn: "#d29922", error: "#f85149", dead: "#f85149",
}
const ACCOUNT_TYPES = ["+30 days old", "+1 year old", "5+ years old", "dump"]
const BASE = () => (import.meta.env.VITE_API_BASE || "http://localhost:8000")

async function genApi(path, method = "GET", body) {
  const token = localStorage.getItem("access_token")
  const r = await fetch(BASE() + "/generator" + path, {
    method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
  if (!r.ok) throw new Error(await r.text())
  return r.json()
}

// ── Styles ───────────────────────────────────────────────────────────────────
const S = {
  wrap:     { fontFamily: "ui-monospace, monospace", background: "#0d1117", color: "#e2e8f0", borderRadius: 8, overflow: "hidden", border: "1px solid #21262d" },
  header:   { background: "#161b22", borderBottom: "1px solid #21262d", padding: "10px 16px", display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" },
  subhdr:   { background: "#0d1117", borderBottom: "1px solid #21262d", padding: "5px 16px", fontSize: 11, color: "#484f58", display: "flex", gap: 14, flexWrap: "wrap" },
  ctrl:     { background: "#161b22", borderBottom: "1px solid #21262d", padding: "8px 16px", display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" },
  body:     { display: "flex", height: 460, overflow: "hidden" },
  sidebar:  { width: 155, background: "#0d1117", borderRight: "1px solid #21262d", padding: "10px 8px", overflowY: "auto", flexShrink: 0 },
  main:     { flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" },
  tabbar:   { borderBottom: "1px solid #21262d", padding: "6px 12px", display: "flex", gap: 6, alignItems: "center" },
  feed:     { flex: 1, overflowY: "auto", padding: "10px 14px", fontSize: 12, lineHeight: 1.65 },
  input:    { background: "#0d1117", border: "1px solid #21262d", color: "#e2e8f0", padding: "5px 8px", borderRadius: 4, fontSize: 12, width: "100%", boxSizing: "border-box" },
  label:    { color: "#8b949e", fontSize: 11, marginBottom: 3, display: "block" },
}

function Btn({ children, color = "#21262d", disabled, onClick, small }) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      padding: small ? "4px 10px" : "6px 14px", borderRadius: 6, border: "none",
      cursor: disabled ? "not-allowed" : "pointer",
      background: disabled ? "#21262d" : color, color: disabled ? "#484f58" : "#fff",
      fontSize: small ? 11 : 12, fontWeight: 500, opacity: disabled ? 0.6 : 1,
    }}>{children}</button>
  )
}

function Tab({ id, cur, label, set }) {
  return (
    <button onClick={() => set(id)} style={{
      padding: "4px 12px", borderRadius: 5, border: "none", cursor: "pointer", fontSize: 11,
      background: cur === id ? "#1f6feb" : "transparent",
      color: cur === id ? "#fff" : "#8b949e",
    }}>{label}</button>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function Generator() {
  const [status, setStatus]       = React.useState(null)
  const [logs, setLogs]           = React.useState([])
  const [accounts, setAccounts]   = React.useState([])
  const [limits, setLimits]       = React.useState(null)
  const [innerTab, setInnerTab]   = React.useState("feed")
  const [search, setSearch]       = React.useState("")
  const [cfg, setCfg]             = React.useState(null)   // loaded from /config
  const [cfgDirty, setCfgDirty]   = React.useState(false)
  const [saving, setSaving]       = React.useState(false)
  const [saveMsg, setSaveMsg]     = React.useState("")
  const feedRef = React.useRef(null)
  const esRef   = React.useRef(null)

  // Load config and status on mount
  React.useEffect(() => {
    genApi("/config").then((c) => setCfg(c)).catch(() => {})
    const poll = () => genApi("/status").then(setStatus).catch(() => {})
    poll()
    const t = setInterval(poll, 2000)
    return () => { clearInterval(t); esRef.current?.close() }
  }, [])

  // Auto-scroll feed
  React.useEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [logs])

  // Load accounts when tab active
  React.useEffect(() => {
    if (innerTab === "accounts") loadAccounts()
  }, [innerTab, search])

  const startStream = React.useCallback(() => {
    esRef.current?.close()
    const token = localStorage.getItem("access_token")
    const es = new EventSource(`${BASE()}/generator/stream?token=${encodeURIComponent(token ?? "")}`)
    esRef.current = es
    es.onmessage = (e) => {
      const d = JSON.parse(e.data)
      if (d.done)      { es.close(); return }
      if (d.heartbeat) return
      setLogs((prev) => [...prev.slice(-499), d])
    }
    es.onerror = () => es.close()
  }, [])

  const handleStart = async () => {
    if (!cfg) return alert("Load config first")
    try { setLogs([]); await genApi("/start", "POST", cfg); startStream() }
    catch (e) { alert(e.message) }
  }

  const handleStop = async () => {
    try { await genApi("/stop", "POST") } catch (e) { alert(e.message) }
  }

  const handleStats = async () => {
    try { setLogs([]); await genApi("/dry-run", "POST"); startStream() }
    catch (e) { alert(e.message) }
  }

  const handleDbCheck = async () => {
    try { setLimits(await genApi("/limits")); setInnerTab("limits") }
    catch (e) { alert(e.message) }
  }

  const handleSaveCfg = async () => {
    setSaving(true)
    try {
      await genApi("/config", "POST", cfg)
      setCfgDirty(false)
      setSaveMsg("Saved ✓")
      setTimeout(() => setSaveMsg(""), 2000)
    } catch (e) { alert(e.message) }
    finally { setSaving(false) }
  }

  const loadAccounts = async () => {
    try { setAccounts(await genApi(`/accounts?search=${encodeURIComponent(search)}`)) }
    catch {}
  }

  const updateCfg = (key, val) => {
    setCfg((prev) => ({ ...prev, [key]: val }))
    setCfgDirty(true)
  }

  const running = status?.running ?? false

  return (
                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <label style={{ ...S.label, margin: 0, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                      <input type="checkbox" checked={cfg.vault_enabled ?? true} onChange={(e) => updateCfg("vault_enabled", e.target.checked)} />
                      Push to vault
                    </label>
                    <label style={{ ...S.label, margin: 0, display: "flex", alignItems: "center", gap: 6, cursor: "pointer" }}>
                      <input type="checkbox" checked={cfg.ssl_verify ?? true} onChange={(e) => updateCfg("ssl_verify", e.target.checked)} />
                      Verify SSL
                    </label>
                  </div>

                  <div>
                    <label style={S.label}>Stop after N consecutive empty (0 = never)</label>
                    <input type="number" min="0" value={cfg.consecutive_empty_stop ?? 5} onChange={(e) => updateCfg("consecutive_empty_stop", parseInt(e.target.value) || 0)} style={{ ...S.input, width: 80 }} />
                  </div>

                  <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                    <Btn color="#238636" disabled={!cfgDirty || saving} onClick={handleSaveCfg}>
                      {saving ? "Saving..." : "Save config"}
                    </Btn>
                    {saveMsg && <span style={{ color: "#3fb950", fontSize: 12 }}>{saveMsg}</span>}
                    {cfgDirty && !saving && <span style={{ color: "#d29922", fontSize: 11 }}>Unsaved changes</span>}
                  </div>
                </div>
              )}
            </div>
          )}

        </div>
      </div>
    </div>
  )
}
