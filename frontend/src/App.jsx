import * as React from "react"
import { ToastProvider } from "@/components/ui/toast"
import LoginPage from "@/pages/LoginPage"
import DashboardPage from "@/pages/DashboardPage"
import { getToken } from "@/lib/api"

export default function App() {
  const [authed, setAuthed] = React.useState(!!getToken())

  return (
    <ToastProvider>
      {authed ? (
        <DashboardPage onLogout={() => setAuthed(false)} />
      ) : (
        <LoginPage onAuthed={() => setAuthed(true)} />
      )}
    </ToastProvider>
  )
}
