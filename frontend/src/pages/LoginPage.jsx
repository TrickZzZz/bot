import * as React from "react"
import { ShieldCheck, Loader2, UserPlus, LogIn } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card"
import { useToast } from "@/components/ui/toast"
import { api, setToken } from "@/lib/api"

export default function LoginPage({ onAuthed }) {
  const { toast } = useToast()
  const [mode, setMode] = React.useState("login")
  const [username, setUsername] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [loading, setLoading] = React.useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setLoading(true)
    try {
      if (mode === "register") {
        await api.register(username, password)
        toast({ title: "Account created", description: "You can log in now." })
      }
      const { access_token } = await api.login(username, password)
      setToken(access_token)
      onAuthed()
    } catch (err) {
      toast({ title: "Authentication failed", description: err.message, variant: "destructive" })
    } finally {
      setLoading(false)
    }
  }

  return (
    // 1. Added bg-[#09090b] here to ensure the dark canvas
    <div className="min-h-screen flex items-center justify-center p-4 relative overflow-hidden bg-[#09090b]">
      
      {/* 2. Put the neon glow back in! */}
      <div className="absolute top-[-20%] left-1/2 -translate-x-1/2 w-[70%] h-[30%] bg-purple-600/80 blur-[160px] rounded-full pointer-events-none mix-blend-screen" />
      
      <Card className="w-full max-w-md relative z-10">
        <CardHeader className="space-y-3">
          <div className="flex items-center gap-2.5">
            <div className="h-10 w-10 rounded-lg bg-primary/15 flex items-center justify-center ring-1 ring-primary/25">
              <ShieldCheck className="h-5 w-5 text-primary" />
            </div>
            <div>
              <CardTitle>Account Manager</CardTitle>
              <CardDescription>
                Sign in to your vault
              </CardDescription>
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <form onSubmit={submit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">Username</Label>
              <Input
                id="username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">Password</Label>
              <Input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                minLength={undefined}
                required
              />
              {mode === "register" && (
                <p className="text-xs text-muted-foreground">Minimum 8 characters.</p>
              )}
            </div>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <><LogIn className="h-4 w-4" /> Sign in</>
              ) }
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}