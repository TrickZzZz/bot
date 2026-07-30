/* eslint-disable */
import * as React from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Card, CardContent } from "@/components/ui/card"
import {
  Zap, Play, Square, BarChart3, Search, Trash2, RotateCcw,
  Clock, KeyRound, CheckCircle2, XCircle, AlertCircle, Circle,
  ShieldCheck, Settings2, Users, UserCog,
} from "lucide-react"

var STATUS_COLOR = {
  ready:  {dot:"bg-emerald-400",  text:"text-emerald-400",  bg:"bg-emerald-500/10",  ring:"ring-emerald-500/20"},
  ok:     {dot:"bg-emerald-400",  text:"text-emerald-400",  bg:"bg-emerald-500/10",  ring:"ring-emerald-500/20"},
  busy:   {dot:"bg-amber-400",    text:"text-amber-400",    bg:"bg-amber-500/10",    ring:"ring-amber-500/20"},
  warn:   {dot:"bg-amber-400",    text:"text-amber-400",    bg:"bg-amber-500/10",    ring:"ring-amber-500/20"},
  error:  {dot:"bg-rose-400",     text:"text-rose-400",     bg:"bg-rose-500/10",     ring:"ring-rose-500/20"},
  dead:   {dot:"bg-rose-400",     text:"text-rose-400",     bg:"bg-rose-500/10",     ring:"ring-rose-500/20"},
}
var LEVEL_STYLE = {
  ok:    {bar:"bg-emerald-400", text:"text-emerald-300"},
  error: {bar:"bg-rose-400",    text:"text-rose-300"},
  warn:  {bar:"bg-amber-400",   text:"text-amber-300"},
  muted: {bar:"bg-zinc-600",    text:"text-zinc-500"},
  info:  {bar:"bg-sky-400",     text:"text-sky-300/90"},
}
var TYPES = ["+30 days old","+1 year old","5+ years old","dump"]
var API_BASE = import.meta.env.VITE_API_BASE || "http://localhost:8000"

function gapi(path, method, body) {
  var token = localStorage.getItem("am_access_token")
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

var HIDDEN_KEY = "generator_hidden_accounts"

function getHiddenSet() {
  try {
    var raw = localStorage.getItem(HIDDEN_KEY)
    return raw ? new Set(JSON.parse(raw)) : new Set()
  } catch (e) { return new Set() }
}
function setHiddenSet(set) {
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(Array.from(set))) } catch (e) {}
}

function formatResetTime(iso) {
  try {
    var d = new Date(iso)
    if (isNaN(d.getTime())) return iso
    var now = new Date()
    var diffMs = d.getTime() - now.getTime()
    if (diffMs < 0) return "now"
    var hrs = Math.floor(diffMs / 3600000)
    var mins = Math.floor((diffMs % 3600000) / 60000)
    var localStr = d.toLocaleString(undefined, {month:"short", day:"numeric", hour:"numeric", minute:"2-digit"})
    return localStr + " · in " + hrs + "h " + mins + "m"
  } catch (e) { return iso }
}

function formatElapsed(sec) {
  var h = Math.floor(sec / 3600)
  var m = Math.floor((sec % 3600) / 60)
  var s = Math.floor(sec % 60)
  function pad(n) { return n < 10 ? "0" + n : "" + n }
  return pad(h) + ":" + pad(m) + ":" + pad(s)
}

// ── small presentational pieces ────────────────────────────────────────────

function StatusPill(props) {
  var running = props.running
  return React.createElement("div", {
    className: "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium " +
      (running ? "bg-emerald-500/10 text-emerald-400 ring-1 ring-emerald-500/20" : "bg-secondary text-muted-foreground")
  },
    React.createElement("span", {className: "relative flex h-1.5 w-1.5"},
      running && React.createElement("span", {className: "animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"}),
      React.createElement("span", {className: "relative inline-flex rounded-full h-1.5 w-1.5 " + (running ? "bg-emerald-400" : "bg-muted-foreground")})
    ),
    running ? "Running" : "Idle"
  )
}

function StatCard(props) {
  return React.createElement(Card, {className: "relative overflow-hidden"},
    React.createElement("div", {className: "absolute left-0 top-0 bottom-0 w-1 " + props.barColor}),
    React.createElement(CardContent, {className: "pt-4 pb-4 pl-5"},
      React.createElement("div", {className: "flex items-center gap-2 text-muted-foreground text-xs font-medium mb-1"},
        React.createElement(props.icon, {className: "h-3.5 w-3.5"}),
        props.label
      ),
      React.createElement("div", {className: "text-2xl font-bold tabular-nums " + props.valueColor}, props.value)
    )
  )
}

function KeyChip(props) {
  var c = STATUS_COLOR[props.status] || {dot:"bg-zinc-500", text:"text-muted-foreground", bg:"bg-secondary", ring:"ring-border"}
  return React.createElement("div", {
    className: "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium ring-1 shrink-0 " + c.bg + " " + c.ring
  },
    React.createElement("span", {className: "relative flex h-1.5 w-1.5"},
      props.status === "busy" && React.createElement("span", {className: "animate-ping absolute inline-flex h-full w-full rounded-full " + c.dot + " opacity-75"}),
      React.createElement("span", {className: "relative inline-flex rounded-full h-1.5 w-1.5 " + c.dot})
    ),
    React.createElement("span", {className: "text-foreground/80"}, "API" + props.num),
    React.createElement("span", {className: c.text}, props.status)
  )
}

function SectionLabel(props) {
  return React.createElement("div", {className: "flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground mb-3 mt-6 first:mt-0"},
    React.createElement(props.icon, {className: "h-3.5 w-3.5"}),
    props.children
  )
}

// ── main component ──────────────────────────────────────────────────────────

export default function Generator() {
  var statusState = React.useState(null); var status = statusState[0]; var setStatus = statusState[1]
  var logsState = React.useState([]); var logs = logsState[0]; var setLogs = logsState[1]
  var accountsState = React.useState([]); var accounts = accountsState[0]; var setAccounts = accountsState[1]
  var limitsState = React.useState(null); var limits = limitsState[0]; var setLimits = limitsState[1]
  var usersState = React.useState([]); var usersList = usersState[0]; var setUsersList = usersState[1]
  var tabState = React.useState("feed"); var tab = tabState[0]; var setTab = tabState[1]
  var searchState = React.useState(""); var search = searchState[0]; var setSearch = searchState[1]
  var cfgState = React.useState(null); var cfg = cfgState[0]; var setCfg = cfgState[1]
  var dirtyState = React.useState(false); var dirty = dirtyState[0]; var setDirty = dirtyState[1]
  var savingState = React.useState(false); var saving = savingState[0]; var setSaving = savingState[1]
  var msgState = React.useState(""); var msg = msgState[0]; var setMsg = msgState[1]
  var elapsedState = React.useState(0); var elapsed = elapsedState[0]; var setElapsed = elapsedState[1]
  var streamingState = React.useState(false); var streaming = streamingState[0]; var setStreaming = streamingState[1]
  var feedRef = React.useRef(null)
  var esRef = React.useRef(null)
  var runStartRef = React.useRef(null)
  var wasRunningRef = React.useRef(false)

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

  // elapsed timer — starts counting when the backend reports running, stops when it doesn't
  React.useEffect(function() {
    var running = !!(status && status.running)
    if (running && !wasRunningRef.current) {
      runStartRef.current = Date.now()
    }
    wasRunningRef.current = running
    if (!running) return
    var iv = setInterval(function() {
      setElapsed(Math.floor((Date.now() - runStartRef.current) / 1000))
    }, 1000)
    return function() { clearInterval(iv) }
  }, [status && status.running])

  React.useEffect(function() {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight
  }, [logs])

  React.useEffect(function() {
    if (tab === "accounts") loadAccounts()
    if (tab === "users") {
      if (!isAdmin) { setTab("feed"); return }
      loadUsers()
    }
  }, [tab, search, isAdmin])

  function startStream() {
    if (esRef.current) esRef.current.close()
    var token = localStorage.getItem("am_access_token")
    var es = new EventSource(API_BASE + "/generator/stream?token=" + encodeURIComponent(token || ""))
    esRef.current = es
    setStreaming(true)
    es.onmessage = function(e) {
      var d = JSON.parse(e.data)
      if (d.done) { setStreaming(false); es.close(); return }
      if (d.heartbeat) return
      setLogs(function(prev) { return prev.slice(-499).concat([d]) })
    }
    es.onerror = function() { setStreaming(false); es.close() }
  }

  function handleStart() {
    if (!cfg) { alert("Config not loaded yet"); return }
    setLogs([]); setElapsed(0)
    gapi("/start", "POST", cfg).then(startStream).catch(function(e) { alert(e.message) })
  }

  function handleStop() {
    gapi("/stop", "POST").catch(function(e) { alert(e.message) })
  }

  function handleStats() {
    setLogs([]); setElapsed(0)
    gapi("/dry-run", "POST").then(startStream).catch(function(e) { alert(e.message) })
    gapi("/limits").then(setLimits).catch(function(){})
  }

  function handleSave() {
    setSaving(true)
    gapi("/config", "POST", cfg).then(function() {
      setDirty(false); setMsg("Saved"); setSaving(false)
      setTimeout(function() { setMsg("") }, 2000)
    }).catch(function(e) { alert(e.message); setSaving(false) })
  }

  function handleChangeUnchangedPasswords() {
    if (!cfg || !cfg.new_password || cfg.new_password.length < 8) {
      alert("Set a new password (8+ characters) in Config first.")
      return
    }
    if (!confirm(
      "Attempt a fresh Roblox login + password change for every vault account that hasn't been changed yet.\n\n" +
      "Some accounts will be skipped — Roblox challenges programmatic logins from this server, so not every login will succeed. That's expected.\n\n" +
      "Progress shows in Live Feed. Continue?"
    )) return
    setLogs([]); setElapsed(0); setTab("feed")
    gapi("/accounts/change-passwords", "POST").then(startStream).catch(function(e) { alert(e.message) })
  }

  function loadAccounts() {
    gapi("/accounts?search=" + encodeURIComponent(search)).then(function(list) {
      var hidden = getHiddenSet()
      setAccounts(list.filter(function(a) { return !hidden.has(a.user) }))
    }).catch(function(){})
  }

  function loadUsers() {
    gapi("/admin/users").then(setUsersList).catch(function(){})
  }

  function clearAccountsList() {
    if (!confirm("Hide all current accounts from this view? New accounts you generate will still show. This does NOT delete anything from the vault.")) return
    gapi("/accounts?limit=5000").then(function(all) {
      var hidden = getHiddenSet()
      all.forEach(function(a) { hidden.add(a.user) })
      setHiddenSet(hidden)
      loadAccounts()
    }).catch(function(e) { alert(e.message) })
  }

  function showAllAccounts() {
    setHiddenSet(new Set())
    loadAccounts()
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
  var keyCount = cfg && cfg.bloxgen_keys && cfg.bloxgen_keys.length ? cfg.bloxgen_keys.length : 7
  var hasHidden = getHiddenSet().size > 0
  var isAdmin = !!(cfg && cfg.is_admin)

  var tabs = [
    {id:"feed", label:"Live Feed", icon: Circle},
    {id:"accounts", label: isAdmin ? "Accounts" : "My Accounts", icon: Users},
    {id:"limits", label:"Limits", icon: BarChart3},
    {id:"config", label:"Config", icon: Settings2},
    isAdmin ? {id:"users", label:"Users", icon: UserCog} : null,
  ].filter(Boolean)

  // key chips: use live key_states if we have them, otherwise show placeholders sized to configured key count
  var keyChips = Object.keys(keyStates).length > 0
    ? Object.keys(keyStates).sort(function(a,b){return Number(a)-Number(b)}).map(function(n) {
        return React.createElement(KeyChip, {key:n, num:n, status:keyStates[n].status})
      })
    : Array.from({length: keyCount}, function(_, i) {
        return React.createElement(KeyChip, {key:i, num:i+1, status:"ready"})
      })

  return React.createElement("div", {className:"space-y-5"},

    // ── Header ──────────────────────────────────────────────────────────────
    React.createElement("div", {className:"flex flex-col sm:flex-row gap-4 sm:items-center sm:justify-between"},
      React.createElement("div", {className:"flex items-center gap-3"},
        React.createElement("div", {className:"h-10 w-10 rounded-xl bg-primary/15 flex items-center justify-center ring-1 ring-primary/25"},
          React.createElement(Zap, {className:"h-5 w-5 text-primary"})
        ),
        React.createElement("div", null,
          React.createElement("div", {className:"flex items-center gap-2.5"},
            React.createElement("h2", {className:"font-semibold text-lg leading-tight"}, "Alt Generator V1"),
            React.createElement(StatusPill, {running: running})
          ),
          React.createElement("p", {className:"text-xs text-muted-foreground mt-0.5"},
            cfg ? cfg.account_type : "loading…"
          )
        )
      ),
      React.createElement("div", {className:"flex gap-2"},
        React.createElement(Button, {
          disabled: running, onClick: handleStart,
          className: "bg-emerald-600 hover:bg-emerald-700 text-white gap-1.5"
        }, React.createElement(Play, {className:"h-3.5 w-3.5"}), "Start"),
        React.createElement(Button, {
          variant:"destructive", disabled:!running, onClick:handleStop, className:"gap-1.5"
        }, React.createElement(Square, {className:"h-3.5 w-3.5"}), "Stop"),
        React.createElement(Button, {
          variant:"outline", disabled:running, onClick:handleStats, className:"gap-1.5"
        }, React.createElement(BarChart3, {className:"h-3.5 w-3.5"}), "Stats"),
      )
    ),

    // ── Key health strip ──────────────────────────────────────────────────
    React.createElement(Card, null,
      React.createElement(CardContent, {className:"py-3 px-4"},
        React.createElement("div", {className:"flex items-center gap-2 mb-2.5 text-xs font-medium text-muted-foreground"},
          React.createElement(KeyRound, {className:"h-3.5 w-3.5"}),
          keyCount + " API " + (keyCount === 1 ? "key" : "keys")
        ),
        React.createElement("div", {className:"flex flex-wrap gap-1.5"}, keyChips)
      )
    ),

    // ── Stat cards ─────────────────────────────────────────────────────────
    React.createElement("div", {className:"grid grid-cols-2 sm:grid-cols-4 gap-3"},
      React.createElement(StatCard, {icon:CheckCircle2, label:"Done", value:done, barColor:"bg-emerald-400", valueColor:"text-emerald-400"}),
      React.createElement(StatCard, {icon:AlertCircle, label:"Empty", value:empty, barColor:"bg-amber-400", valueColor:"text-amber-400"}),
      React.createElement(StatCard, {icon:XCircle, label:"Fails", value:fails, barColor:"bg-rose-400", valueColor:"text-rose-400"}),
      React.createElement(StatCard, {icon:Clock, label:"Elapsed", value:formatElapsed(elapsed), barColor:"bg-sky-400", valueColor:"text-sky-400"})
    ),

    // ── Tabs ───────────────────────────────────────────────────────────────
    React.createElement("div", {className:"flex gap-1 border-b border-border"},
      tabs.map(function(t) {
        var active = tab === t.id
        return React.createElement("button", {
          key: t.id,
          onClick: function() { setTab(t.id) },
          className: "flex items-center gap-1.5 px-3.5 py-2 text-sm font-medium border-b-2 transition-colors -mb-px " +
            (active ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground")
        },
          React.createElement(t.icon, {className:"h-3.5 w-3.5"}),
          t.label
        )
      })
    ),

    // ── FEED ─────────────────────────────────────────────────────────────
    tab === "feed" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-0"},
        React.createElement("div", {className:"flex items-center justify-between px-4 py-2.5 border-b border-border"},
          React.createElement("div", {className:"flex items-center gap-2"},
            React.createElement("span", {className:"h-1.5 w-1.5 rounded-full " + (streaming ? "bg-emerald-400 animate-pulse" : "bg-zinc-600")}),
            React.createElement("span", {className:"text-xs text-muted-foreground font-medium tracking-wide"}, "LIVE FEED")
          ),
          React.createElement(Button, {size:"sm", variant:"ghost", onClick:function(){setLogs([])}, className:"h-7 gap-1.5 text-xs"},
            React.createElement(Trash2, {className:"h-3 w-3"}), "Clear"
          )
        ),
        React.createElement("div", {
          ref: feedRef,
          className: "bg-black/20",
          style: {height:420, overflowY:"auto", padding:"10px 0", fontFamily:"ui-monospace,SFMono-Regular,Menlo,monospace", fontSize:12.5, lineHeight:1.8}
        },
          logs.length === 0 && React.createElement("div", {className:"px-4 py-8 text-center"},
            React.createElement("p", {className:"text-muted-foreground text-sm"}, "Nothing here yet."),
            React.createElement("p", {className:"text-muted-foreground/60 text-xs mt-1"}, "Press Start to run the generator, or Stats to check keys and stock.")
          ),
          logs.map(function(line, i) {
            var st = LEVEL_STYLE[line.level] || LEVEL_STYLE.info
            return React.createElement("div", {key:i, className:"flex px-4 hover:bg-white/[0.02]"},
              React.createElement("span", {className:"w-0.5 shrink-0 rounded-full mr-2.5 " + st.bar}),
              React.createElement("span", {className: st.text + " whitespace-pre-wrap break-all"}, line.msg)
            )
          })
        )
      )
    ),

    // ── ACCOUNTS ─────────────────────────────────────────────────────────
    tab === "accounts" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4 space-y-3"},
        React.createElement("div", {className:"flex gap-2 flex-wrap items-center"},
          React.createElement("div", {className:"relative flex-1 min-w-[180px]"},
            React.createElement(Search, {className:"absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground"}),
            React.createElement(Input, {value:search, onChange:function(e){setSearch(e.target.value)}, placeholder:"Search accounts...", className:"pl-8"})
          ),
          isAdmin && React.createElement(Button, {size:"sm", variant:"outline", onClick:clearAccountsList, className:"gap-1.5"},
            React.createElement(Trash2, {className:"h-3.5 w-3.5"}), "Clear list"
          ),
          isAdmin && hasHidden && React.createElement(Button, {size:"sm", variant:"ghost", onClick:showAllAccounts, className:"gap-1.5"},
            React.createElement(RotateCcw, {className:"h-3.5 w-3.5"}), "Show all"
          ),
          isAdmin && React.createElement(Button, {size:"sm", variant:"outline", disabled:running, onClick:handleChangeUnchangedPasswords, className:"gap-1.5"},
            React.createElement(ShieldCheck, {className:"h-3.5 w-3.5"}), "Change unchanged passwords"
          ),
          React.createElement("div", {className:"flex items-center gap-1.5 text-xs text-muted-foreground whitespace-nowrap ml-auto"},
            isAdmin && hasHidden && React.createElement("span", {className:"px-1.5 py-0.5 rounded bg-secondary text-[10px] font-medium"}, "filtered"),
            accounts.length + " accounts"
          )
        ),
        accounts.length === 0
          ? React.createElement("div", {className:"py-10 text-center"},
              React.createElement(Users, {className:"h-8 w-8 mx-auto text-muted-foreground/40 mb-2"}),
              React.createElement("p", {className:"text-sm text-muted-foreground"},
                isAdmin
                  ? (hasHidden ? "No new accounts yet — generate some or press Show all." : "No accounts match your search.")
                  : "You haven't generated any accounts yet — press Start to make some.")
            )
          : React.createElement("div", {style:{overflowX:"auto"}},
              React.createElement("table", {className:"w-full text-sm"},
                React.createElement("thead", null,
                  React.createElement("tr", {className:"border-b border-border text-left text-muted-foreground"},
                    ["User","Pass","Date","Age","Type","PW?","Vault?"].map(function(h) {
                      return React.createElement("th", {key:h, className:"font-medium px-3 py-2 text-xs uppercase tracking-wide"}, h)
                    })
                  )
                ),
                React.createElement("tbody", null,
                  accounts.map(function(a, i) {
                    return React.createElement("tr", {key:i, className:"border-b border-border/60 last:border-0 hover:bg-secondary/30 transition-colors"},
                      React.createElement("td", {className:"px-3 py-2 text-foreground/90"}, a.user),
                      React.createElement("td", {className:"px-3 py-2 font-mono text-xs text-muted-foreground"}, a.pass),
                      React.createElement("td", {className:"px-3 py-2 text-muted-foreground text-xs"}, a.date),
                      React.createElement("td", {className:"px-3 py-2 text-muted-foreground"}, a.age),
                      React.createElement("td", {className:"px-3 py-2"},
                        a.type && a.type !== "—"
                          ? React.createElement("span", {className:"text-xs px-1.5 py-0.5 rounded bg-secondary text-foreground/70"}, a.type)
                          : React.createElement("span", {className:"text-muted-foreground"}, "—")
                      ),
                      React.createElement("td", {className:"px-3 py-2"},
                        a.pw_changed
                          ? React.createElement(CheckCircle2, {className:"h-3.5 w-3.5 text-emerald-400"})
                          : React.createElement("span", {className:"text-muted-foreground"}, "—")
                      ),
                      React.createElement("td", {className:"px-3 py-2"},
                        a.vault_pushed
                          ? React.createElement(CheckCircle2, {className:"h-3.5 w-3.5 text-emerald-400"})
                          : React.createElement("span", {className:"text-muted-foreground"}, "—")
                      )
                    )
                  })
                )
              )
            )
      )
    ),

    // ── LIMITS ───────────────────────────────────────────────────────────
    tab === "limits" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4"},
        !limits
          ? React.createElement("div", {className:"py-8 text-center text-sm text-muted-foreground"}, "Loading stock status…")
          : React.createElement(React.Fragment, null,
              limits.stock_error && React.createElement("div", {className:"flex items-center gap-2 text-sm text-rose-400 mb-3 bg-rose-500/10 rounded-md px-3 py-2"},
                React.createElement(AlertCircle, {className:"h-4 w-4 shrink-0"}),
                "Stock check failed: " + limits.stock_error
              ),
              React.createElement("div", {className:"space-y-2"},
                (limits.types || TYPES).map(function(t) {
                  var s = limits.stock && limits.stock[t]
                  var q = limits.quota && limits.quota[t]
                  var has = s ? s.available : null
                  var remaining = q ? q.remainingGenerations : null
                  var dailyLimit = q ? q.dailyLimit : 0
                  var pct = (remaining !== null && dailyLimit > 0) ? Math.round((remaining / dailyLimit) * 100) : null
                  var barColor = remaining === null ? "bg-zinc-700" : remaining > 0 ? "bg-emerald-400" : "bg-rose-400"
                  return React.createElement("div", {key:t, className:"rounded-lg border border-border p-3"},
                    React.createElement("div", {className:"flex items-center justify-between mb-2"},
                      React.createElement("span", {className:"text-sm font-medium"}, t),
                      React.createElement("span", {
                        className: "text-xs px-2 py-0.5 rounded-full font-medium " +
                          (has === null ? "bg-secondary text-muted-foreground" : has ? "bg-emerald-500/10 text-emerald-400" : "bg-rose-500/10 text-rose-400")
                      }, has === null ? "N/A" : has ? "in stock" : "no stock")
                    ),
                    React.createElement("div", {className:"flex items-center gap-3"},
                      React.createElement("div", {className:"flex-1 h-1.5 rounded-full bg-secondary overflow-hidden"},
                        pct !== null && React.createElement("div", {className:"h-full rounded-full " + barColor, style:{width: Math.max(pct,3) + "%"}})
                      ),
                      React.createElement("span", {className:"text-xs text-muted-foreground tabular-nums whitespace-nowrap"},
                        remaining === null ? "—" : (remaining + " left · " + q.generationsToday + "/" + q.dailyLimit + " used")
                      )
                    )
                  )
                })
              ),
              limits.reset_time && React.createElement("p", {className:"text-xs text-muted-foreground mt-4 flex items-center gap-1.5"},
                React.createElement(Clock, {className:"h-3 w-3"}),
                "Resets " + formatResetTime(limits.reset_time)
              )
            )
      )
    ),

    // ── CONFIG ───────────────────────────────────────────────────────────
    tab === "config" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-5"},
        !cfg
          ? React.createElement("div", {className:"py-8 text-center text-sm text-muted-foreground"}, "Loading config…")
          : React.createElement("div", {className:"max-w-lg"},

              React.createElement(SectionLabel, {icon: Zap}, "Generation"),
              React.createElement("div", {className:"space-y-4"},
                React.createElement("div", {className:"space-y-1.5"},
                  React.createElement("label", {className:"text-sm font-medium"}, "Account type"),
                  React.createElement("select", {
                    value: cfg.account_type || "+30 days old",
                    onChange: function(e) { upd("account_type", e.target.value) },
                    className: "w-full rounded-md border border-input bg-background px-3 py-2 text-sm"
                  }, TYPES.map(function(t) { return React.createElement("option", {key:t, value:t}, t) }))
                ),
                React.createElement("div", {className:"space-y-1.5"},
                  React.createElement("label", {className:"text-sm font-medium"}, "New password"),
                  React.createElement(Input, {value:cfg.new_password||"", onChange:function(e){upd("new_password",e.target.value)}, placeholder:"Leave blank to keep original"})
                ),
                React.createElement("div", {className:"space-y-1.5"},
                  React.createElement("label", {className:"text-sm font-medium"}, "Target count"),
                  React.createElement("p", {className:"text-xs text-muted-foreground -mt-0.5 mb-1.5"}, "0 = run until stopped manually"),
                  React.createElement(Input, {value:cfg.target_count||0, onChange:function(e){upd("target_count",parseInt(e.target.value)||0)}, style:{width:120}})
                )
              ),

              React.createElement(SectionLabel, {icon: ShieldCheck}, "Vault & safety"),
              React.createElement("div", {className:"space-y-4"},
                React.createElement("div", {className:"flex gap-5"},
                  isAdmin
                    ? React.createElement("label", {className:"flex items-center gap-2 text-sm cursor-pointer"},
                        React.createElement("input", {type:"checkbox", checked:cfg.vault_enabled||false, onChange:function(e){upd("vault_enabled",e.target.checked)}}),
                        "Push to vault"
                      )
                    : React.createElement("span", {className:"text-xs text-muted-foreground italic"}, "Vault access is managed by the site owner"),
                  React.createElement("label", {className:"flex items-center gap-2 text-sm cursor-pointer"},
                    React.createElement("input", {type:"checkbox", checked:cfg.ssl_verify||false, onChange:function(e){upd("ssl_verify",e.target.checked)}}),
                    "Verify SSL"
                  )
                ),
                React.createElement("div", {className:"space-y-1.5"},
                  React.createElement("label", {className:"text-sm font-medium"}, "Stop after N empty in a row"),
                  React.createElement("p", {className:"text-xs text-muted-foreground -mt-0.5 mb-1.5"}, "0 = never auto-stop on empty stock"),
                  React.createElement(Input, {value:cfg.consecutive_empty_stop||5, onChange:function(e){upd("consecutive_empty_stop",parseInt(e.target.value)||0)}, style:{width:80}})
                )
              ),

              isAdmin && React.createElement(SectionLabel, {icon: KeyRound}, "API keys"),
              isAdmin && React.createElement("div", {className:"space-y-1.5"},
                React.createElement("label", {className:"text-sm font-medium"}, "Bloxgen API keys"),
                React.createElement("p", {className:"text-xs text-muted-foreground -mt-0.5 mb-1.5"}, "One per line"),
                React.createElement("textarea", {
                  value: (cfg.bloxgen_keys||[]).join("\n"),
                  onChange: function(e) { upd("bloxgen_keys", e.target.value.split("\n").map(function(k){return k.trim()}).filter(Boolean)) },
                  rows: 6,
                  placeholder: "BLOX-XXXXXXXXXXXXXXXX",
                  className: "w-full rounded-md border border-input bg-background px-3 py-2 text-sm font-mono resize-y"
                })
              ),

              React.createElement("div", {className:"flex items-center gap-3 mt-6 pt-4 border-t border-border"},
                React.createElement(Button, {disabled:!dirty||saving, onClick:handleSave}, saving?"Saving...":"Save config"),
                msg && React.createElement("span", {className:"text-sm text-emerald-400 flex items-center gap-1"},
                  React.createElement(CheckCircle2, {className:"h-3.5 w-3.5"}), msg
                ),
                dirty && !saving && React.createElement("span", {className:"text-sm text-amber-400"}, "Unsaved changes")
              )
            )
      )
    ),

    // ── USERS (admin only) ───────────────────────────────────────────────
    tab === "users" && React.createElement(Card, null,
      React.createElement(CardContent, {className:"p-4 space-y-3"},
        React.createElement("div", {className:"flex items-center justify-between"},
          React.createElement("span", {className:"text-xs text-muted-foreground font-medium tracking-wide"}, "REGISTERED USERS"),
          React.createElement(Button, {size:"sm", variant:"ghost", onClick:loadUsers, className:"gap-1.5"},
            React.createElement(RotateCcw, {className:"h-3.5 w-3.5"}), "Refresh"
          )
        ),
        usersList.length === 0
          ? React.createElement("p", {className:"text-sm text-muted-foreground py-6 text-center"}, "No users found (or still loading)")
          : React.createElement("table", {className:"w-full text-sm"},
              React.createElement("thead", null,
                React.createElement("tr", {className:"border-b border-border text-left text-muted-foreground"},
                  ["Username","Role"].map(function(h) {
                    return React.createElement("th", {key:h, className:"font-medium px-3 py-2 text-xs uppercase tracking-wide"}, h)
                  })
                )
              ),
              React.createElement("tbody", null,
                usersList.map(function(u, i) {
                  return React.createElement("tr", {key:i, className:"border-b border-border/60 last:border-0"},
                    React.createElement("td", {className:"px-3 py-2 text-foreground/90"}, u.username),
                    React.createElement("td", {className:"px-3 py-2"},
                      u.is_admin
                        ? React.createElement("span", {className:"text-xs px-2 py-0.5 rounded-full bg-primary/15 text-primary font-medium"}, "Admin")
                        : React.createElement("span", {className:"text-xs px-2 py-0.5 rounded-full bg-secondary text-muted-foreground"}, "Standard")
                    )
                  )
                })
              )
            ),
        React.createElement("p", {className:"text-xs text-muted-foreground pt-2"},
          "Admin status is set by the ADMIN_USERNAMES environment variable on the server — it can't be changed from here."
        )
      )
    )
  )
}
