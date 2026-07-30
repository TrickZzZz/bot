/* eslint-disable */
import * as React from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent } from "@/components/ui/card"
import { Zap, RotateCcw, Square, BarChart2, Settings } from "lucide-react"

var LEVEL_COLOR = {ok:"#3fb950",error:"#f85149",warn:"#d29922",muted:"#6b7280",info:"#94a3b8"}
var KEY_COLOR = {ready:"#3fb950",busy:"#d29922",ok:"#3fb950",warn:"#d29922",error:"#f85149",dead:"#f85149"}
var TYPES = ["+30 days old","+1 year old","5+ years old","dump"]
var API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000"

function gapi(path, method, body) {
  var token = localStorage.getItem("access_token")
  var opts = {
    method: method || "GET",
    headers: {Authorization: "Bearer " + token, "Content-Type": "application/json"},
  }
  if (body !== undefined) opts.body = JSON.stringify(body)
  return fetch(API_BASE + "/generator" + path, opts).then(function(r) {
    if (!r.ok) return r.text().then(function(t) { throw new Error(t) })
    return r.json()
  })
}

export default function Generator() {
  var statusState = React.useState(null); var status = statusState[0]; var setStatus = statusState[1]
  var logsState = React.useState([]); var logs = logsState[0]; var setLogs = logsState[1]
  var accountsState = React.useState([]); var accounts = accountsState[0]; var setAccounts = accountsState[1]
  var limitsState = React.useState(null); var limits = limitsState[0]; var setLimits = limitsState[1]
  var tabState = React.useState("feed"); var tab = tabState[0]; var setTab = tabState[1]
  var searchState = React.useState(""); var search = searchState[0]; var setSearch = searchState[1]
  var cfgState = React.useState(null); var cfg = cfgState[0]; var setCfg = cfgState[1]
  var dirtyState = React.useState(false); var dirty = dirtyState[0]; var setDirty = dirtyState[1]
  var savingState = React.useState(false); var saving = savingState[0]; var setSaving = savingState[1]
  var msgState = React.useState(""); var msg = msgState[0]; var setMsg = msgState[1]
  var feedRef = React.useRef(null)
  var esRef = React.useRef(null)

  React.useEffect(function() {
    gapi("/config").then(setCfg).catch(function() {
      setCfg({account_type:"+30 days old",new_password:"",target_count:0,vault_enabled:true,ssl_verify:true,bloxgen_keys:[],consecutive_empty_stop:5})
    })
    function poll() { gapi("/status").then(setStatus).catch(function(){}) }
    function pollLimits() { gapi("/limits").then(setLimits).catch(function(){}) }
    poll(); pollLimits()
    var t = setInterval(poll, 2000)
    var tl = setInterval(pollLimits, 120000)
    return function() { clearInterval(t); clearInterval(tl); if (esRef.current) esRef.current.close() }
  }, [])

  React.useEffect(function() {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [logs])

  React.useEffect(function() {
    if (tab === "accounts") loadAccounts()
  }, [tab, search])

  function startStream() {
    if (esRef.current) esRef.current.close()
    var token = localStorage.getItem("access_token")
    var es = new EventSource(API_BASE + "/generator/stream?token=" + encodeURIComponent(token || ""))
    esRef.current = es
    es.onmessage = function(e) {
      var d = JSON.parse(e.data)
      if (d.done) { es.close(); return }
      if (d.heartbeat) return
      setLogs(function(prev) { return prev.slice(-499).concat([d]) })
    }
    es.onerror = function() { es.close() }
  }

  function handleStart() {
    if (!cfg) { alert("Config not loaded yet"); return }
    setLogs([])
    gapi("/start", "POST", cfg).then(startStream).catch(function(e) { alert(e.message) })
  }

  function handleStop() {
    gapi("/stop", "POST").catch(function(e) { alert(e.message) })
  }

  function handleStats() {
    setLogs([])
    gapi("/dry-run", "POST").then(startStream).catch(function(e) { alert(e.message) })
    gapi("/limits").then(setLimits).catch(function(){})
  }

  function handleSave() {
    setSaving(true)
    gapi("/config", "POST", cfg).then(function() {
      setDirty(false); setMsg("Saved ✓"); setSaving(false)
      setTimeout(function() { setMsg("") }, 2000)
    }).catch(function(e) { alert(e.message); setSaving(false) })
  }

  function loadAccounts() {
    gapi("/accounts?search=" + encodeURIComponent(search)).then(setAccounts).catch(function(){})
  }

  function upd(key, val) {
    setCfg(function(p) { var n = Object.assign({}, p); n[key] = val; return n })
    setDirty(true)
  }

  var running = !!(status && status.running)
  var done = status ? status.done : 0
  var empty = status ? status.stock_empty : 0
  var fails = status ? status.fails : 0
  var keyStates = status ? (status.key_states || {}) : {}
  var keyCount = cfg && cfg.bloxgen_keys ? cfg.bloxgen_keys.length : 7

  // ── Render ──────────────────────────────────────────────────────────────────
  var tabs = [
    {id:"feed", label:"Live Feed"},
    {id:"accounts", label:"Accounts"},
    {id:"limits", label:"Limits"},
    {id:"config", label:"Config"},
  ]

  return React.createElement("div", {className:"space-y-6"},
    // Page header — matches accounts page style
    React.createElement("div", {className:"flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between"},
      React.createElement("div", {className:"flex items-center gap-3"},
        React.createElement("div", {className:"h-9 w-9 rounded-lg bg-primary/15 flex items-center justify-center ring-1 ring-primary/25"},
          React.createElement(Zap, {className:"h-5 w-5 text-primary"})
        ),
        React.createElement("div", null,
          React.createElement("h2", {className:"font-semibold text-lg leading-tight"}, "Alt Generator V1"),
          React.createElement("p", {className:"text-xs text-muted-foreground"},
            running ? "● running — done: " + done : "● idle"
          )
        )
      ),
      // Control buttons
      React.createElement("div", {className:"flex gap-2 flex-wrap"},
        React.createElement(Button, {size:"sm", disabled:running, onClick:handleStart, className:"bg-green-600 hover:bg-green-700 text-white"}, "Start"),
        React.createElement(Button, {size:"sm", variant:"destructive", disabled:!running, onClick:handleStop}, "Stop"),
        React.createElement(Button, {size:"sm", variant:"outline", disabled:running, onClick:handleStats}, "Stats"),
      )
    ),

    // Stats row
    React.createElement("div", {className:"grid grid-cols-3 gap-3"},
      React.createElement(Card, null,
        React.createElement(CardContent, {className:"pt-4 pb-4 text-center"},
          React.createElement("div", {className:"text-2xl font-bold text-green-400"}, done),
          React.createElement("div", {className:"text-xs text-muted-foreground mt-1"}, "DONE")
        )
      ),
      React.createElement(Card, null,
        React.createElement(CardContent, {className:"pt-4 pb-4 text-center"},
          React.createElement("div", {className:"text-2xl font-bold text-yellow-400"}, empty),
          React.createElement("div", {className:"text-xs text-muted-foreground mt-1"}, "EMPTY")
        )
      ),
      React.createElement(Card, null,
        React.createElement(CardContent, {className:"pt-4 pb-4 text-center"},
          React.createElement("div", {className:"text-2xl font-bold text-red-400"}, fails),
          React.createElement("div", {className:"text-xs text-muted-foreground mt-1"}, "FAILS")
        )
      )
    ),

    // Tab nav
    React.createElement("div", {className:"flex gap-1 border-b border-border pb-0"},
      tabs.map(function(t) {
        return React.createElement("button", {
          key: t.id,
          onClick: function() { setTab(t.id) },
          className: "px-4 py-2 text-sm font-medium border-b-2 transition-colors " + (tab === t.id ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")
        }, t.label)
      })
    ),

    // Tab content
    tab === "feed" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-0"},
        React.createElement("div", {className:"flex items-center justify-between px-4 py-2 border-b border-border"},
          React.createElement("span", {className:"text-xs text-muted-foreground font-medium tracking-wider"}, "LIVE FEED"),
          React.createElement(Button, {size:"sm", variant:"ghost", onClick:function(){setLogs([])}}, "Clear")
        ),
        React.createElement("div", {
          ref: feedRef,
          style: {height:400, overflowY:"auto", padding:"12px 16px", fontFamily:"ui-monospace,monospace", fontSize:12, lineHeight:1.7}
        },
          logs.length === 0 && React.createElement("p", {className:"text-muted-foreground text-sm"}, "Press Start or Stats to begin..."),
          logs.map(function(line, i) {
            return React.createElement("div", {key:i, style:{color:LEVEL_COLOR[line.level]||"#94a3b8", marginBottom:1, whiteSpace:"pre-wrap", wordBreak:"break-all"}}, line.msg)
          })
        )
      )
    ),

    tab === "accounts" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4 space-y-3"},
        React.createElement("div", {className:"flex gap-2"},
          React.createElement(Input, {value:search, onChange:function(e){setSearch(e.target.value)}, placeholder:"Search accounts..."}),
          React.createElement("span", {className:"text-sm text-muted-foreground self-center whitespace-nowrap"}, accounts.length + " accounts")
        ),
        React.createElement("div", {style:{overflowX:"auto"}},
          React.createElement("table", {className:"w-full text-sm"},
            React.createElement("thead", null,
              React.createElement("tr", {className:"border-b border-border text-left text-muted-foreground"},
                ["User","Pass","Date","Age","Type","PW?","Vault?"].map(function(h) {
                  return React.createElement("th", {key:h, className:"font-medium px-3 py-2"}, h)
                })
              )
            ),
            React.createElement("tbody", null,
              accounts.map(function(a, i) {
                return React.createElement("tr", {key:i, className:"border-b border-border/60 last:border-0 hover:bg-secondary/30 transition-colors"},
                  React.createElement("td", {className:"px-3 py-2 text-muted-foreground"}, a.user),
                  React.createElement("td", {className:"px-3 py-2 font-mono text-muted-foreground"}, a.pass),
                  React.createElement("td", {className:"px-3 py-2 text-muted-foreground"}, a.date),
                  React.createElement("td", {className:"px-3 py-2"}, a.age),
                  React.createElement("td", {className:"px-3 py-2"}, a.type),
                  React.createElement("td", {className:"px-3 py-2", style:{color:a.pw_changed?"#3fb950":"#6b7280"}}, a.pw_changed?"✓":"—"),
                  React.createElement("td", {className:"px-3 py-2", style:{color:a.vault_pushed?"#3fb950":"#6b7280"}}, a.vault_pushed?"✓":"—")
                )
              })
            )
          )
        )
      )
    ),

    tab === "limits" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4"},
        !limits
          ? React.createElement("p", {className:"text-sm text-muted-foreground"}, "Loading stock status...")
          : React.createElement("table", {className:"w-full text-sm"},
              React.createElement("thead", null,
                React.createElement("tr", {className:"border-b border-border text-left text-muted-foreground"},
                  ["Type","Stock","Status"].map(function(h) {
                    return React.createElement("th", {key:h, className:"font-medium px-3 py-2"}, h)
                  })
                )
              ),
              React.createElement("tbody", null,
                (limits.types || TYPES).map(function(t) {
                  var s = limits.stock && limits.stock[t]
                  var has = s ? s.available : null
                  var statusEl = has === null
                    ? React.createElement("span", {className:"text-muted-foreground"}, "N/A")
                    : has
                      ? React.createElement("span", {className:"text-green-400"}, "in stock")
                      : React.createElement("span", {className:"text-red-400"}, "no stock")
                  return React.createElement("tr", {key:t, className:"border-b border-border/60 last:border-0"},
                    React.createElement("td", {className:"px-3 py-2"}, t),
                    React.createElement("td", {className:"px-3 py-2 text-muted-foreground"}, s && s.count ? s.count : "—"),
                    React.createElement("td", {className:"px-3 py-2"}, statusEl)
                  )
                })
              )
            )
      )
    ),

    tab === "config" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4"},
        !cfg
          ? React.createElement("p", {className:"text-sm text-muted-foreground"}, "Loading config...")
          : React.createElement("div", {className:"space-y-4 max-w-lg"},
              React.createElement("div", {className:"space-y-1"},
                React.createElement("label", {className:"text-sm font-medium"}, "Account type"),
                React.createElement("select", {
                  value: cfg.account_type || "+30 days old",
                  onChange: function(e) { upd("account_type", e.target.value) },
                  className: "w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                },
                  TYPES.map(function(t) { return React.createElement("option", {key:t, value:t}, t) })
                )
              ),
              React.createElement("div", {className:"space-y-1"},
                React.createElement("label", {className:"text-sm font-medium"}, "New password"),
                React.createElement(Input, {value:cfg.new_password||"", onChange:function(e){upd("new_password",e.target.value)}, placeholder:"Leave blank to keep original"})
              ),
              React.createElement("div", {className:"space-y-1"},
                React.createElement("label", {className:"text-sm font-medium"}, "Target count (0 = unlimited)"),
                React.createElement(Input, {value:cfg.target_count||0, onChange:function(e){upd("target_count",parseInt(e.target.value)||0)}, style:{width:120}})
              ),
              React.createElement("div", {className:"space-y-1"},
                React.createElement("label", {className:"text-sm font-medium"}, "Bloxgen API keys (one per line)"),
                React.createElement("textarea", {
                  value: (cfg.bloxgen_keys||[]).join("\n"),
                  onChange: function(e) { upd("bloxgen_keys", e.target.value.split("\n").map(function(k){return k.trim()}).filter(Boolean)) },
                  rows: 6,
                  placeholder: "BLOX-XXXXXXXXXXXXXXXX",
                  className: "w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono resize-y"
                })
              ),
              React.createElement("div", {className:"flex gap-4"},
                React.createElement("label", {className:"flex items-center gap-2 text-sm cursor-pointer"},
                  React.createElement("input", {type:"checkbox", checked:cfg.vault_enabled||false, onChange:function(e){upd("vault_enabled",e.target.checked)}}),
                  "Push to vault"
                ),
                React.createElement("label", {className:"flex items-center gap-2 text-sm cursor-pointer"},
                  React.createElement("input", {type:"checkbox", checked:cfg.ssl_verify||false, onChange:function(e){upd("ssl_verify",e.target.checked)}}),
                  "Verify SSL"
                )
              ),
              React.createElement("div", {className:"space-y-1"},
                React.createElement("label", {className:"text-sm font-medium"}, "Stop after N empty in a row (0 = never)"),
                React.createElement(Input, {value:cfg.consecutive_empty_stop||5, onChange:function(e){upd("consecutive_empty_stop",parseInt(e.target.value)||0)}, style:{width:80}})
              ),
              React.createElement("div", {className:"flex items-center gap-3"},
                React.createElement(Button, {disabled:!dirty||saving, onClick:handleSave}, saving?"Saving...":"Save config"),
                msg && React.createElement("span", {className:"text-sm text-green-400"}, msg),
                dirty && !saving && React.createElement("span", {className:"text-sm text-yellow-400"}, "Unsaved changes")
              )
            )
      )
    )
  )
}
