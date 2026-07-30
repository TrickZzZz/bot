/* eslint-disable */
import * as React from "react"

var LEVEL_COLOR = {ok:"#3fb950",error:"#f85149",warn:"#d29922",muted:"#484f58",info:"#8b949e"}
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

var inputStyle = {background:"#0d1117",border:"1px solid #21262d",color:"#e2e8f0",padding:"5px 8px",borderRadius:4,fontSize:12,width:"100%",boxSizing:"border-box"}
var labelStyle = {color:"#8b949e",fontSize:11,marginBottom:3,display:"block"}

function Btn(props) {
  var bg = props.disabled ? "#21262d" : (props.color || "#21262d")
  var col = props.disabled ? "#484f58" : "#fff"
  return React.createElement("button", {
    onClick: props.onClick,
    disabled: props.disabled,
    style: {padding:"6px 14px",borderRadius:6,border:"none",cursor:props.disabled?"not-allowed":"pointer",background:bg,color:col,fontSize:12,fontWeight:500,opacity:props.disabled?0.6:1}
  }, props.children)
}

function TabBtn(props) {
  var bg = props.active ? "#1f6feb" : "transparent"
  var col = props.active ? "#fff" : "#8b949e"
  return React.createElement("button", {
    onClick: props.onClick,
    style: {padding:"4px 12px",borderRadius:5,border:"none",cursor:"pointer",fontSize:11,background:bg,color:col}
  }, props.label)
}

export default function Generator() {
  var statusState = React.useState(null)
  var status = statusState[0]; var setStatus = statusState[1]
  var logsState = React.useState([])
  var logs = logsState[0]; var setLogs = logsState[1]
  var accountsState = React.useState([])
  var accounts = accountsState[0]; var setAccounts = accountsState[1]
  var limitsState = React.useState(null)
  var limits = limitsState[0]; var setLimits = limitsState[1]
  var tabState = React.useState("feed")
  var tab = tabState[0]; var setTab = tabState[1]
  var searchState = React.useState("")
  var search = searchState[0]; var setSearch = searchState[1]
  var cfgState = React.useState(null)
  var cfg = cfgState[0]; var setCfg = cfgState[1]
  var dirtyState = React.useState(false)
  var dirty = dirtyState[0]; var setDirty = dirtyState[1]
  var savingState = React.useState(false)
  var saving = savingState[0]; var setSaving = savingState[1]
  var msgState = React.useState("")
  var msg = msgState[0]; var setMsg = msgState[1]
  var feedRef = React.useRef(null)
  var esRef = React.useRef(null)

  React.useEffect(function() {
    gapi("/config").then(setCfg).catch(function() {
      setCfg({account_type:"+30 days old",new_password:"",target_count:0,vault_enabled:true,ssl_verify:true,bloxgen_keys:[],consecutive_empty_stop:5})
    })
    function poll() { gapi("/status").then(setStatus).catch(function(){}) }
    poll()
    var t = setInterval(poll, 2000)
    return function() { clearInterval(t); if (esRef.current) esRef.current.close() }
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
    if (!cfg) { alert("Load config first"); return }
    setLogs([])
    gapi("/start", "POST", cfg).then(startStream).catch(function(e) { alert(e.message) })
  }

  function handleStop() {
    gapi("/stop", "POST").catch(function(e) { alert(e.message) })
  }

  function handleStats() {
    setLogs([])
    gapi("/dry-run", "POST").then(startStream).catch(function(e) { alert(e.message) })
  }

  function handleSave() {
    setSaving(true)
    gapi("/config", "POST", cfg).then(function() {
      setDirty(false); setMsg("Saved"); setSaving(false)
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
  var vaultStock = status && status.vault_stock >= 0 ? status.vault_stock : "—"
  var done = status ? status.done : 0
  var empty = status ? status.stock_empty : 0
  var fails = status ? status.fails : 0
  var keyStates = status ? (status.key_states || {}) : {}
  var keyCount = cfg && cfg.bloxgen_keys ? cfg.bloxgen_keys.length : 7

  var wrapStyle = {fontFamily:"ui-monospace,monospace",background:"#0d1117",color:"#e2e8f0",borderRadius:8,overflow:"hidden",border:"1px solid #21262d"}
  var hdrStyle = {background:"#161b22",borderBottom:"1px solid #21262d",padding:"10px 16px",display:"flex",alignItems:"center",gap:12,flexWrap:"wrap"}
  var subStyle = {background:"#0d1117",borderBottom:"1px solid #21262d",padding:"5px 16px",fontSize:11,color:"#484f58",display:"flex",gap:14,flexWrap:"wrap"}
  var ctrlStyle = {background:"#161b22",borderBottom:"1px solid #21262d",padding:"8px 16px",display:"flex",gap:8,flexWrap:"wrap",alignItems:"center"}
  var bodyStyle = {display:"flex",height:460,overflow:"hidden"}
  var sideStyle = {width:155,background:"#0d1117",borderRight:"1px solid #21262d",padding:"10px 8px",overflowY:"auto",flexShrink:0}
  var mainStyle = {flex:1,display:"flex",flexDirection:"column",overflow:"hidden"}
  var tbarStyle = {borderBottom:"1px solid #21262d",padding:"6px 12px",display:"flex",gap:6,alignItems:"center"}
  var feedStyle = {flex:1,overflowY:"auto",padding:"10px 14px",fontSize:12,lineHeight:1.65}

  var keyRows = Object.keys(keyStates).length > 0
    ? Object.entries(keyStates).map(function(entry) {
        var n = entry[0]; var ks = entry[1]
        var col = KEY_COLOR[ks.status] || "#484f58"
        return React.createElement("div", {key:n, style:{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"4px 6px",borderRadius:4,background:"#161b22",marginBottom:3,fontSize:10}},
          React.createElement("span", {style:{color:"#8b949e"}}, "API" + n),
          React.createElement("span", {style:{color:col,background:"#0d1117",padding:"1px 5px",borderRadius:3}}, ks.status)
        )
      })
    : Array.from({length:keyCount}, function(_, i) {
        return React.createElement("div", {key:i, style:{display:"flex",justifyContent:"space-between",padding:"4px 6px",borderRadius:4,background:"#161b22",marginBottom:3,fontSize:10}},
          React.createElement("span", {style:{color:"#484f58"}}, "API" + (i+1)),
          React.createElement("span", {style:{color:"#3fb950",background:"#0d1117",padding:"1px 5px",borderRadius:3}}, "ready")
        )
      })

  var feedContent = tab === "feed" && React.createElement("div", {style:mainStyle},
    React.createElement("div", {style:tbarStyle},
      React.createElement("span", {style:{fontSize:10,color:"#484f58",letterSpacing:1,flex:1}}, "LIVE FEED"),
      React.createElement("button", {onClick:function(){setLogs([])}, style:{background:"none",border:"1px solid #21262d",color:"#484f58",padding:"2px 8px",borderRadius:4,cursor:"pointer",fontSize:10}}, "clear")
    ),
    React.createElement("div", {ref:feedRef, style:feedStyle},
      logs.length === 0 && React.createElement("div", {style:{color:"#30363d",paddingTop:16}}, "Press Start or Stats to begin..."),
      logs.map(function(line, i) {
        return React.createElement("div", {key:i, style:{color:LEVEL_COLOR[line.level]||"#8b949e",marginBottom:1,whiteSpace:"pre-wrap",wordBreak:"break-all"}}, line.msg)
      })
    )
  )

  var acctContent = tab === "accounts" && React.createElement("div", {style:mainStyle},
    React.createElement("div", {style:tbarStyle},
      React.createElement("input", {value:search, onChange:function(e){setSearch(e.target.value)}, placeholder:"Search...", style:Object.assign({},inputStyle,{flex:1,padding:"3px 8px"})}),
      React.createElement("span", {style:{fontSize:10,color:"#484f58",paddingLeft:8,whiteSpace:"nowrap"}}, accounts.length + " accounts")
    ),
    React.createElement("div", {style:{flex:1,overflowY:"auto"}},
      React.createElement("table", {style:{width:"100%",borderCollapse:"collapse",fontSize:11}},
        React.createElement("thead", {style:{position:"sticky",top:0,background:"#161b22"}},
          React.createElement("tr", null,
            ["User","Pass","Date","Age","Type","PW?","Vault?"].map(function(h) {
              return React.createElement("th", {key:h, style:{padding:"5px 8px",color:"#484f58",fontWeight:500,textAlign:"left",borderBottom:"1px solid #21262d"}}, h)
            })
          )
        ),
        React.createElement("tbody", null,
          accounts.map(function(a, i) {
            return React.createElement("tr", {key:i, style:{background:i%2===0?"#0d1117":"#161b22"}},
              React.createElement("td", {style:{padding:"4px 8px",color:"#8b949e"}}, a.user),
              React.createElement("td", {style:{padding:"4px 8px",color:"#484f58",fontFamily:"monospace"}}, a.pass),
              React.createElement("td", {style:{padding:"4px 8px",color:"#484f58"}}, a.date),
              React.createElement("td", {style:{padding:"4px 8px",color:"#8b949e"}}, a.age),
              React.createElement("td", {style:{padding:"4px 8px",color:"#8b949e"}}, a.type),
              React.createElement("td", {style:{padding:"4px 8px",color:a.pw_changed?"#3fb950":"#484f58"}}, a.pw_changed?"✓":"—"),
              React.createElement("td", {style:{padding:"4px 8px",color:a.vault_pushed?"#3fb950":"#484f58"}}, a.vault_pushed?"✓":"—")
            )
          })
        )
      )
    )
  )

  var limitsContent = tab === "limits" && React.createElement("div", {style:{flex:1,overflowY:"auto",padding:14}},
    !limits
      ? React.createElement("div", {style:{color:"#484f58",fontSize:12}}, "No limits data yet — press Stats to load...")
      : React.createElement("table", {style:{width:"100%",borderCollapse:"collapse",fontSize:12}},
          React.createElement("thead", null,
            React.createElement("tr", null,
              ["Type","Stock","Status"].map(function(h) {
                return React.createElement("th", {key:h, style:{padding:"5px 8px",color:"#484f58",fontWeight:500,textAlign:"left",borderBottom:"1px solid #21262d"}}, h)
              })
            )
          ),
          React.createElement("tbody", null,
            (limits.types || TYPES).map(function(t) {
              var s = limits.stock && limits.stock[t]
              var has = s ? s.available : null
              var statusEl = has === null
                ? React.createElement("span", {style:{color:"#484f58"}}, "N/A")
                : has
                  ? React.createElement("span", {style:{color:"#3fb950"}}, "in stock")
                  : React.createElement("span", {style:{color:"#f85149"}}, "no stock")
              return React.createElement("tr", {key:t},
                React.createElement("td", {style:{padding:"6px 8px",color:"#8b949e"}}, t),
                React.createElement("td", {style:{padding:"6px 8px",color:"#8b949e"}}, s && s.count ? s.count : "—"),
                React.createElement("td", {style:{padding:"6px 8px"}}, statusEl)
              )
            })
          )
        )
  )

  var cfgContent = tab === "config" && React.createElement("div", {style:{flex:1,overflowY:"auto",padding:16}},
    !cfg
      ? React.createElement("div", {style:{color:"#484f58",fontSize:12}}, "Loading...")
      : React.createElement("div", {style:{display:"grid",gap:14,maxWidth:520}},
          React.createElement("div", null,
            React.createElement("label", {style:labelStyle}, "Account type"),
            React.createElement("select", {value:cfg.account_type||"+30 days old", onChange:function(e){upd("account_type",e.target.value)}, style:Object.assign({},inputStyle,{cursor:"pointer"})},
              TYPES.map(function(t) { return React.createElement("option", {key:t, value:t}, t) })
            )
          ),
          React.createElement("div", null,
            React.createElement("label", {style:labelStyle}, "New password (blank = keep original)"),
            React.createElement("input", {value:cfg.new_password||"", onChange:function(e){upd("new_password",e.target.value)}, placeholder:"NewPassword123", style:inputStyle})
          ),
          React.createElement("div", null,
            React.createElement("label", {style:labelStyle}, "Target count (0 = unlimited)"),
            React.createElement("input", {value:cfg.target_count||0, onChange:function(e){upd("target_count",parseInt(e.target.value)||0)}, style:Object.assign({},inputStyle,{width:120})})
          ),
          React.createElement("div", null,
            React.createElement("label", {style:labelStyle}, "Bloxgen API keys (one per line)"),
            React.createElement("textarea", {value:(cfg.bloxgen_keys||[]).join("\n"), onChange:function(e){upd("bloxgen_keys",e.target.value.split("\n").map(function(k){return k.trim()}).filter(Boolean))}, rows:6, placeholder:"BLOX-XXXXXXXXXXXXXXXX", style:Object.assign({},inputStyle,{resize:"vertical"})})
          ),
          React.createElement("div", {style:{display:"flex",alignItems:"center",gap:16}},
            React.createElement("label", {style:{color:"#8b949e",fontSize:11,display:"flex",alignItems:"center",gap:6,cursor:"pointer"}},
              React.createElement("input", {type:"checkbox", checked:cfg.vault_enabled||false, onChange:function(e){upd("vault_enabled",e.target.checked)}}),
              "Push to vault"
            ),
            React.createElement("label", {style:{color:"#8b949e",fontSize:11,display:"flex",alignItems:"center",gap:6,cursor:"pointer"}},
              React.createElement("input", {type:"checkbox", checked:cfg.ssl_verify||false, onChange:function(e){upd("ssl_verify",e.target.checked)}}),
              "Verify SSL"
            )
          ),
          React.createElement("div", null,
            React.createElement("label", {style:labelStyle}, "Stop after N empty in a row (0 = never)"),
            React.createElement("input", {value:cfg.consecutive_empty_stop||5, onChange:function(e){upd("consecutive_empty_stop",parseInt(e.target.value)||0)}, style:Object.assign({},inputStyle,{width:80})})
          ),
          React.createElement("div", {style:{display:"flex",alignItems:"center",gap:10}},
            React.createElement(Btn, {color:"#238636", disabled:!dirty||saving, onClick:handleSave}, saving?"Saving...":"Save config"),
            msg && React.createElement("span", {style:{color:"#3fb950",fontSize:12}}, msg),
            dirty && !saving && React.createElement("span", {style:{color:"#d29922",fontSize:11}}, "Unsaved")
          )
        )
  )

  return React.createElement("div", {style:wrapStyle},
    React.createElement("div", {style:hdrStyle},
      React.createElement("span", {style:{color:"#58a6ff",fontWeight:700,fontSize:13,letterSpacing:2}}, "DELTACORE"),
      React.createElement("span", {style:{color:"#30363d",fontSize:12}}, "ALT GEN"),
      React.createElement("span", {style:{color:running?"#3fb950":"#484f58",fontSize:11}}, running?"● running":"● idle"),
      React.createElement("span", {style:{marginLeft:"auto",color:"#8b949e",fontSize:12}}, "VAULT " + vaultStock)
    ),
    React.createElement("div", {style:subStyle},
      React.createElement("span", null, "Done: ", React.createElement("b", {style:{color:"#3fb950"}}, done)),
      React.createElement("span", null, "Empty: " + empty),
      React.createElement("span", null, "Fails: " + fails),
      React.createElement("span", null, "Type: " + (cfg && cfg.account_type ? cfg.account_type : "—"))
    ),
    React.createElement("div", {style:ctrlStyle},
      React.createElement(Btn, {color:"#238636", disabled:running, onClick:handleStart}, "Start"),
      React.createElement(Btn, {color:"#da3633", disabled:!running, onClick:handleStop}, "Stop"),
      React.createElement(Btn, {color:"#1f6feb", disabled:running, onClick:handleStats}, "Stats"),
      React.createElement("div", {style:{marginLeft:"auto",display:"flex",gap:4}},
        React.createElement(TabBtn, {active:tab==="feed", label:"Feed", onClick:function(){setTab("feed")}}),
        React.createElement(TabBtn, {active:tab==="accounts", label:"Accounts", onClick:function(){setTab("accounts")}}),
        React.createElement(TabBtn, {active:tab==="limits", label:"Limits", onClick:function(){setTab("limits")}}),
        React.createElement(TabBtn, {active:tab==="config", label:"Config", onClick:function(){setTab("config")}})
      )
    ),
    React.createElement("div", {style:bodyStyle},
      React.createElement("div", {style:sideStyle},
        React.createElement("div", {style:{fontSize:10,color:"#484f58",marginBottom:6,letterSpacing:1}}, "API KEYS"),
        keyRows
      ),
      feedContent || acctContent || limitsContent || cfgContent
    )
  )
}
