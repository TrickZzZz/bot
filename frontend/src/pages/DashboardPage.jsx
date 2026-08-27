import * as React from "react"
import {
  ShieldCheck, Plus, Search, Pencil, Trash2, Eye, EyeOff, Copy, Check,
  Upload, LogOut, Loader2, KeyRound, Filter, AlertTriangle,
} from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent } from "@/components/ui/card"
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog"
import { useToast } from "@/components/ui/toast"
import { api, clearToken } from "@/lib/api"

const EMPTY_FORM = { username: "", password: "", account_type: "+30 days old", region: "" }

const TYPE_COLOURS = {
  "+30 days old":  "bg-blue-500/15 text-blue-400 ring-blue-500/25",
  "+1 year old":   "bg-purple-500/15 text-purple-400 ring-purple-500/25",
  "5+ years old":  "bg-amber-500/15 text-amber-400 ring-amber-500/25",
  "dump":          "bg-zinc-500/15 text-zinc-400 ring-zinc-500/25",
}
const typeBadge = (t) => TYPE_COLOURS[t] ?? "bg-zinc-500/15 text-zinc-400 ring-zinc-500/25"

function PasswordCell({ value }) {
  const [show, setShow] = React.useState(false)
  const [copied, setCopied] = React.useState(false)
  const copy = async () => {
    await navigator.clipboard.writeText(value)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-mono text-sm tracking-tight">
        {show ? value : "•".repeat(Math.min(value.length, 12))}
      </span>
      <button onClick={() => setShow(s => !s)} className="text-muted-foreground hover:text-foreground p-1">
        {show ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
      </button>
      <button onClick={copy} className="text-muted-foreground hover:text-foreground p-1">
        {copied ? <Check className="h-3.5 w-3.5 text-green-400" /> : <Copy className="h-3.5 w-3.5" />}
      </button>
    </div>
  )
}

export default function DashboardPage({ onLogout }) {
  const { toast } = useToast()
  const [accounts, setAccounts]   = React.useState([])
  const [loading, setLoading]     = React.useState(true)
  const [search, setSearch]       = React.useState("")
  const [typeFilter, setTypeFilter] = React.useState("")
  const [allTypes, setAllTypes]   = React.useState([])
  const [me, setMe]               = React.useState(null)

  const [editOpen, setEditOpen]   = React.useState(false)
  const [editing, setEditing]     = React.useState(null)
  const [form, setForm]           = React.useState(EMPTY_FORM)
  const [saving, setSaving]       = React.useState(false)

  const [deleteTarget, setDeleteTarget] = React.useState(null)
  const [wipeOpen, setWipeOpen]   = React.useState(false)
  const [wiping, setWiping]       = React.useState(false)
  const [importOpen, setImportOpen] = React.useState(false)
  const [importText, setImportText] = React.useState("")
  const [importing, setImporting] = React.useState(false)

  const load = React.useCallback(async (q = "", t = "") => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (q) params.set("search", q)
      if (t) params.set("account_type", t)
      const data = await api.listAccounts(q, t)
      setAccounts(data)
    } catch (err) {
      toast({ title: "Could not load accounts", description: err.message, variant: "destructive" })
      if (err.message.includes("log in")) onLogout()
    } finally {
      setLoading(false)
    }
  }, [toast, onLogout])

  React.useEffect(() => {
    api.me().then(setMe).catch(() => {})
    api.listTypes().then(setAllTypes).catch(() => {})
    load()
  }, [load])

  React.useEffect(() => {
    const t = setTimeout(() => load(search, typeFilter), 250)
    return () => clearTimeout(t)
  }, [search, typeFilter, load])

  const openCreate = () => { setEditing(null); setForm(EMPTY_FORM); setEditOpen(true) }
  const openEdit   = (acc) => {
    setEditing(acc)
    setForm({ username: acc.username, password: acc.password, account_type: acc.account_type, region: acc.region || "" })
    setEditOpen(true)
  }

  const save = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      if (editing) {
        await api.updateAccount(editing.id, form)
        toast({ title: "Account updated" })
      } else {
        await api.createAccount(form)
        toast({ title: "Account created" })
      }
      setEditOpen(false)
      load(search, typeFilter)
    } catch (err) {
      toast({ title: "Save failed", description: err.message, variant: "destructive" })
    } finally {
      setSaving(false)
    }
  }

  const confirmDelete = async () => {
    try {
      await api.deleteAccount(deleteTarget.id)
      toast({ title: "Account deleted" })
      setDeleteTarget(null)
      load(search, typeFilter)
    } catch (err) {
      toast({ title: "Delete failed", description: err.message, variant: "destructive" })
    }
  }

  const confirmWipe = async () => {
    setWiping(true)
    try {
      await api.wipeAccounts()
      toast({ title: "Vault wiped", description: "All accounts deleted." })
      setWipeOpen(false)
      load()
    } catch (err) {
      toast({ title: "Wipe failed", description: err.message, variant: "destructive" })
    } finally {
      setWiping(false)
    }
  }

  const runImport = async () => {
    setImporting(true)
    try {
      const parsed = parseImport(importText)
      if (!parsed.length) { toast({ title: "Nothing to import", variant: "destructive" }); return }
      const result = await api.bulkImport(parsed)
      toast({
        title: "Import complete",
        description: `${result.created} created/updated, ${result.failed} failed.`,
        variant: result.failed > 0 ? "destructive" : "default",
      })
      setImportOpen(false); setImportText(""); load(search, typeFilter)
    } catch (err) {
      toast({ title: "Import failed", description: err.message, variant: "destructive" })
    } finally { setImporting(false) }
  }

  const byType = React.useMemo(() => {
    const m = {}
    accounts.forEach(a => { m[a.account_type] = (m[a.account_type] || 0) + 1 })
    return m
  }, [accounts])

  return (
    <div className="min-h-screen">
      <header className="border-b border-border sticky top-0 z-20 bg-background/80 backdrop-blur-md">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <div className="h-9 w-9 rounded-lg bg-primary/15 flex items-center justify-center ring-1 ring-primary/25">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>
            <div>
              <h1 className="font-semibold leading-tight">DeltaCore Vault</h1>
              {me && <p className="text-xs text-muted-foreground">Welcome, {me.username}</p>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" className="text-destructive hover:text-destructive" onClick={() => setWipeOpen(true)}>
              <Trash2 className="h-4 w-4" /> Wipe vault
            </Button>
            <Button variant="ghost" size="sm" onClick={() => { clearToken(); onLogout() }}>
              <LogOut className="h-4 w-4" /> Log out
            </Button>
          </div>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">
        {/* Stats bar */}
        <div className="flex flex-wrap gap-2 mb-6">
          {Object.entries(byType).map(([t, n]) => (
            <button key={t}
              onClick={() => setTypeFilter(typeFilter === t ? "" : t)}
              className={`px-3 py-1 rounded-full text-xs font-medium ring-1 transition-opacity ${typeBadge(t)} ${typeFilter && typeFilter !== t ? "opacity-40" : "opacity-100"}`}>
              {t} · {n}
            </button>
          ))}
        </div>

        {/* Toolbar */}
        <div className="flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between mb-4">
          <div className="flex gap-2 flex-1">
            <div className="relative flex-1 max-w-xs">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
              <Input placeholder="Search username…" value={search}
                onChange={e => setSearch(e.target.value)} className="pl-9" />
            </div>
            {allTypes.length > 0 && (
              <select value={typeFilter} onChange={e => setTypeFilter(e.target.value)}
                className="rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                <option value="">All types</option>
                {allTypes.map(t => <option key={t} value={t}>{t}</option>)}
              </select>
            )}
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={() => setImportOpen(true)}>
              <Upload className="h-4 w-4" /> Bulk import
            </Button>
            <Button onClick={openCreate}><Plus className="h-4 w-4" /> New account</Button>
          </div>
        </div>

        {/* Table */}
        {loading ? (
          <div className="flex items-center justify-center py-20 text-muted-foreground">
            <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading…
          </div>
        ) : accounts.length === 0 ? (
          <Card><CardContent className="py-16 text-center">
            <KeyRound className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
            <p className="text-muted-foreground">
              {search || typeFilter ? "No accounts match your filters." : "No accounts yet."}
            </p>
          </CardContent></Card>
        ) : (
          <Card className="overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-border text-left text-muted-foreground">
                    <th className="font-medium px-4 py-3">Username</th>
                    <th className="font-medium px-4 py-3">Password</th>
                    <th className="font-medium px-4 py-3">Type</th>
                    <th className="font-medium px-4 py-3">Region</th>
                    <th className="font-medium px-4 py-3 text-right">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {accounts.map(acc => (
                    <tr key={acc.id} className="border-b border-border/60 last:border-0 hover:bg-secondary/30 transition-colors">
                      <td className="px-4 py-3 font-medium">{acc.username}</td>
                      <td className="px-4 py-3"><PasswordCell value={acc.password} /></td>
                      <td className="px-4 py-3">
                        <span className={`px-2 py-0.5 rounded-full text-xs font-medium ring-1 ${typeBadge(acc.account_type)}`}>
                          {acc.account_type}
                        </span>
                      </td>
                      <td className="px-4 py-3 text-muted-foreground text-xs">{acc.region || "—"}</td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-1">
                          <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(acc)}>
                            <Pencil className="h-4 w-4" />
                          </Button>
                          <Button variant="ghost" size="icon" className="h-8 w-8 text-destructive hover:text-destructive" onClick={() => setDeleteTarget(acc)}>
                            <Trash2 className="h-4 w-4" />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Card>
        )}
        <p className="text-xs text-muted-foreground mt-4">{accounts.length} account{accounts.length !== 1 ? "s" : ""}</p>
      </main>

      {/* Create / Edit */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit account" : "New account"}</DialogTitle>
          </DialogHeader>
          <form onSubmit={save} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label>Username</Label>
                <Input value={form.username} onChange={e => setForm({...form, username: e.target.value})} required />
              </div>
              <div className="space-y-2">
                <Label>Password</Label>
                <Input value={form.password} onChange={e => setForm({...form, password: e.target.value})} required />
              </div>
              <div className="space-y-2">
                <Label>Account type</Label>
                <select value={form.account_type} onChange={e => setForm({...form, account_type: e.target.value})}
                  className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                  {["+30 days old","+1 year old","5+ years old","dump"].map(t =>
                    <option key={t} value={t}>{t}</option>)}
                </select>
              </div>
              <div className="space-y-2">
                <Label>Region</Label>
                <Input value={form.region} placeholder="GB, US…"
                  onChange={e => setForm({...form, region: e.target.value.toUpperCase()})} maxLength={10} />
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button type="button" variant="outline" onClick={() => setEditOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={saving}>
                {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : editing ? "Save" : "Create"}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete */}
      <Dialog open={!!deleteTarget} onOpenChange={o => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete account?</DialogTitle>
            <DialogDescription>Permanently removes credentials for <strong>{deleteTarget?.username}</strong>.</DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button variant="destructive" onClick={confirmDelete}>Delete</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Wipe vault */}
      <Dialog open={wipeOpen} onOpenChange={setWipeOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2 text-destructive">
              <AlertTriangle className="h-5 w-5" /> Wipe entire vault?
            </DialogTitle>
            <DialogDescription>This permanently deletes ALL accounts. This cannot be undone.</DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setWipeOpen(false)}>Cancel</Button>
            <Button variant="destructive" onClick={confirmWipe} disabled={wiping}>
              {wiping ? <Loader2 className="h-4 w-4 animate-spin" /> : "Wipe all accounts"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Bulk import */}
      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Bulk import</DialogTitle>
            <DialogDescription>CSV (username,password,account_type,region) or JSON array.</DialogDescription>
          </DialogHeader>
          <textarea value={importText} onChange={e => setImportText(e.target.value)} rows={10}
            placeholder={'username,password,account_type,region\ncooluser123,DeltaCore,+30 days old,GB'}
            className="w-full rounded-md border border-input bg-secondary/50 px-3 py-2 text-sm font-mono focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring resize-y" />
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setImportOpen(false)}>Cancel</Button>
            <Button onClick={runImport} disabled={importing || !importText.trim()}>
              {importing ? <Loader2 className="h-4 w-4 animate-spin" /> : <><Upload className="h-4 w-4" /> Import</>}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function parseImport(text) {
  const trimmed = text.trim()
  if (!trimmed) return []
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    const data = JSON.parse(trimmed)
    const arr = Array.isArray(data) ? data : [data]
    return arr.filter(r => r.username && r.password).map(r => ({
      username:     String(r.username),
      password:     String(r.password),
      account_type: String(r.account_type || "+30 days old"),
      region:       String(r.region || ""),
    }))
  }
  const lines = trimmed.split(/\r?\n/).filter(l => l.trim())
  if (lines.length < 2) return []
  const headers = splitCsvLine(lines[0]).map(h => h.trim().toLowerCase())
  const idx = name => headers.indexOf(name)
  return lines.slice(1).flatMap(line => {
    const cells = splitCsvLine(line)
    const username = cells[idx("username")]?.trim()
    const password = cells[idx("password")]?.trim()
    if (!username || !password) return []
    return [{ username, password,
      account_type: cells[idx("account_type")]?.trim() || "+30 days old",
      region: cells[idx("region")]?.trim() || "",
    }]
  })
}

function splitCsvLine(line) {
  const result = []; let cur = ""; let inQ = false
  for (let i = 0; i < line.length; i++) {
    const ch = line[i]
    if (ch === '"') { if (inQ && line[i+1] === '"') { cur += '"'; i++ } else inQ = !inQ }
    else if (ch === ',' && !inQ) { result.push(cur); cur = "" }
    else cur += ch
  }
  result.push(cur); return result
}
