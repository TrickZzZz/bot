import * as React from "react"
import {
  ShieldCheck, Plus, Search, Pencil, Trash2, Eye, EyeOff, Copy, Check,
  Upload, LogOut, Loader2, KeyRound,
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

const EMPTY_FORM = { username: "", password: "" }

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
      <button onClick={() => setShow((s) => !s)} className="text-muted-foreground hover:text-foreground p-1">
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
  const [page, setPage]         = React.useState("accounts")
  const [accounts, setAccounts] = React.useState([])
  const [loading, setLoading]   = React.useState(true)
  const [search, setSearch]     = React.useState("")
  const [me, setMe]             = React.useState(null)

  const [editOpen, setEditOpen]       = React.useState(false)
  const [editing, setEditing]         = React.useState(null)
  const [form, setForm]               = React.useState(EMPTY_FORM)
  const [saving, setSaving]           = React.useState(false)
  const [deleteTarget, setDeleteTarget] = React.useState(null)
  const [importOpen, setImportOpen]   = React.useState(false)
  const [importText, setImportText]   = React.useState("")
  const [importing, setImporting]     = React.useState(false)

  const load = React.useCallback(async (q = "") => {
    setLoading(true)
    try {
      const data = await api.listAccounts(q)
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
    load()
  }, [load])

  React.useEffect(() => {
    if (page !== "accounts") return
    const t = setTimeout(() => load(search), 250)
    return () => clearTimeout(t)
  }, [search, load, page])

  const openCreate = () => { setEditing(null); setForm(EMPTY_FORM); setEditOpen(true) }
  const openEdit   = (acc) => { setEditing(acc); setForm({ username: acc.username, password: acc.password }); setEditOpen(true) }

  const save = async (e) => {
    e.preventDefault(); setSaving(true)
    try {
      const payload = { username: form.username, password: form.password }
      if (editing) { await api.updateAccount(editing.id, payload); toast({ title: "Account updated" }) }
      else         { await api.createAccount(payload);              toast({ title: "Account created" }) }
      setEditOpen(false); load(search)
    } catch (err) { toast({ title: "Save failed", description: err.message, variant: "destructive" })
    } finally { setSaving(false) }
  }

  const confirmDelete = async () => {
    try {
      await api.deleteAccount(deleteTarget.id)
      toast({ title: "Account deleted" })
      setDeleteTarget(null); load(search)
    } catch (err) { toast({ title: "Delete failed", description: err.message, variant: "destructive" }) }
  }

  const runImport = async () => {
    setImporting(true)
    try {
      const parsed = parseImport(importText)
      if (parsed.length === 0) { toast({ title: "Nothing to import", description: "No valid rows found.", variant: "destructive" }); return }
      const result = await api.bulkImport(parsed)
      toast({ title: "Import complete", description: `${result.created} created, ${result.failed} failed.`, variant: result.failed > 0 ? "destructive" : "default" })
      setImportOpen(false); setImportText(""); load(search)
    } catch (err) { toast({ title: "Import failed", description: err.message, variant: "destructive" })
    } finally { setImporting(false) }
  }

  const logout = () => { clearToken(); onLogout() }

  return (
    <div className="min-h-screen">
      {/* Header */}
      <header className="border-b border-border sticky top-0 z-20 bg-background/80 backdrop-blur-md">
        <div className="max-w-6xl mx-auto px-4 h-16 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2.5">
              <div className="h-9 w-9 rounded-lg bg-primary/15 flex items-center justify-center ring-1 ring-primary/25">
                <ShieldCheck className="h-5 w-5 text-primary" />
              </div>
              <div>
                <h1 className="font-semibold leading-tight">Account Manager</h1>
                {me && <p className="text-xs text-muted-foreground">Welcome, {me.username}!</p>}
              </div>
            </div>
            {/* Page tabs */}
            <nav className="flex gap-1 ml-4">
              <button
                onClick={() => setPage("accounts")}
                className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${page === "accounts" ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:text-foreground hover:bg-secondary"}`}
              >
                Accounts
              </button>
            </nav>
          </div>
          <Button variant="ghost" size="sm" onClick={logout}>
            <LogOut className="h-4 w-4" /> Log out
          </Button>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-4 py-8">

        {/* ── ACCOUNTS PAGE ── */}
        {page === "accounts" && (
          <>
            <div className="flex flex-col sm:flex-row gap-3 sm:items-center sm:justify-between mb-6">
              <div className="relative flex-1 max-w-xs">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                <Input placeholder="Search username..." value={search} onChange={(e) => setSearch(e.target.value)} className="pl-9" />
              </div>
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setImportOpen(true)}><Upload className="h-4 w-4" /> Bulk import</Button>
                <Button onClick={openCreate}><Plus className="h-4 w-4" /> New account</Button>
              </div>
            </div>

            {loading ? (
              <div className="flex items-center justify-center py-20 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin mr-2" /> Loading...
              </div>
            ) : accounts.length === 0 ? (
              <Card>
                <CardContent className="py-16 text-center">
                  <KeyRound className="h-10 w-10 mx-auto text-muted-foreground mb-3" />
                  <p className="text-muted-foreground">
                    {search ? "No accounts match your search." : "No accounts yet. Add one or import in bulk."}
                  </p>
                </CardContent>
              </Card>
            ) : (
              <Card className="overflow-hidden">
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-border text-left text-muted-foreground">
                        <th className="font-medium px-4 py-3">Username</th>
                        <th className="font-medium px-4 py-3">Password</th>
                        <th className="font-medium px-4 py-3 text-right">Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {accounts.map((acc) => (
                        <tr key={acc.id} className="border-b border-border/60 last:border-0 hover:bg-secondary/30 transition-colors">
                          <td className="px-4 py-3 text-muted-foreground">{acc.username}</td>
                          <td className="px-4 py-3"><PasswordCell value={acc.password} /></td>
                          <td className="px-4 py-3">
                            <div className="flex items-center justify-end gap-1">
                              <Button variant="ghost" size="icon" className="h-8 w-8" onClick={() => openEdit(acc)}><Pencil className="h-4 w-4" /></Button>
                              <Button variant="ghost" size="icon" className="h-8 w-8 text-destructive hover:text-destructive" onClick={() => setDeleteTarget(acc)}><Trash2 className="h-4 w-4" /></Button>
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
          </>
        )}
      </main>

      {/* Dialogs — unchanged */}
      <Dialog open={editOpen} onOpenChange={setEditOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editing ? "Edit account" : "New account"}</DialogTitle>
            <DialogDescription>{editing ? "Update the stored credentials." : "Store a new set of credentials."}</DialogDescription>
          </DialogHeader>
          <form onSubmit={save} className="space-y-4">
            <div className="grid grid-cols-2 gap-4">
              <div className="space-y-2">
                <Label htmlFor="acc_username">Username</Label>
                <Input id="acc_username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} required />
              </div>
              <div className="space-y-2">
                <Label htmlFor="acc_password">Password</Label>
                <Input id="acc_password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} required />
              </div>
            </div>
            <DialogFooter className="gap-2">
              <Button type="button" variant="outline" onClick={() => setEditOpen(false)}>Cancel</Button>
              <Button type="submit" disabled={saving}>{saving ? <Loader2 className="h-4 w-4 animate-spin" /> : editing ? "Save changes" : "Create"}</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={!!deleteTarget} onOpenChange={(o) => !o && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete account?</DialogTitle>
            <DialogDescription>This permanently removes the credentials for <span className="text-foreground font-medium">{deleteTarget?.username}</span>. This cannot be undone.</DialogDescription>
          </DialogHeader>
          <DialogFooter className="gap-2">
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>Cancel</Button>
            <Button variant="destructive" onClick={confirmDelete}>Delete</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog open={importOpen} onOpenChange={setImportOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Bulk import</DialogTitle>
            <DialogDescription>Paste CSV or JSON. CSV columns: username, password (header row required).</DialogDescription>
          </DialogHeader>
          <textarea value={importText} onChange={(e) => setImportText(e.target.value)} rows={10}
            placeholder={'username,password\nme@example.com,s3cret\n— or JSON —\n[{"username":"me","password":"s3cret"}]'}
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
    return (Array.isArray(data) ? data : [data]).filter((r) => r.username && r.password).map((r) => ({ username: String(r.username), password: String(r.password) }))
  }
  const lines = trimmed.split(/\r?\n/).filter((l) => l.trim())
  if (lines.length < 2) return []
  const headers = splitCsvLine(lines[0]).map((h) => h.trim().toLowerCase())
  const idx = (name) => headers.indexOf(name)
  return lines.slice(1).map((l) => { const c = splitCsvLine(l); return { username: c[idx("username")]?.trim(), password: c[idx("password")]?.trim() } }).filter((r) => r.username && r.password)
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
