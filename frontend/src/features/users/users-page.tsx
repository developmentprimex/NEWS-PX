"use client"

import * as React from "react"
import { useAuth } from "@/lib/auth"
import type { User } from "@/lib/types"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"

type Account = User & { active: boolean; createdAt: string | null }
type Props = { user: Pick<User, "role"> | null; apiFetch: (path: string, init?: RequestInit) => Promise<Response> }

export function UsersPage() {
  const { user, apiFetch } = useAuth()
  return <UsersWorkspace user={user} apiFetch={apiFetch} />
}

export function UsersWorkspace({ user, apiFetch }: Props) {
  const [accounts, setAccounts] = React.useState<Account[]>([])
  const [loading, setLoading] = React.useState(true)
  const [email, setEmail] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState("")
  const [notice, setNotice] = React.useState("")
  const [resetId, setResetId] = React.useState<string | null>(null)
  const [resetPassword, setResetPassword] = React.useState("")

  const load = React.useCallback(async () => {
    const response = await apiFetch("/auth/users")
    if (!response.ok) throw new Error("Could not load users. Try again.")
    setAccounts(await response.json())
  }, [apiFetch])
  React.useEffect(() => {
    if (user?.role !== "ADMIN") return
    void load().catch((cause) => setError(cause.message)).finally(() => setLoading(false))
  }, [load, user?.role])

  const mutate = async (path: string, method: string, body: object, success: string) => {
    if (busy) return false
    setBusy(true); setError(""); setNotice("")
    try {
      const response = await apiFetch(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })
      const payload = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "Could not save this account. Check the email and password.")
      setAccounts(current => method === "POST" ? [...current, payload as Account] : current.map(account => account.id === payload.id ? payload as Account : account))
      setNotice(success)
      return true
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Could not save this account.")
      return false
    } finally { setBusy(false) }
  }

  if (user?.role !== "ADMIN") return <div className="p-8 text-sm">Administrator access required.</div>
  return <main className="mx-auto max-w-5xl px-5 py-9 text-[#1d2b23] sm:px-9">
    <p className="text-xs uppercase tracking-widest text-[#83958a]">Workspace</p>
    <h1 className="mt-2 text-3xl font-semibold">Users</h1>
    <p className="mt-2 text-sm text-[#758179]">Create private accounts. Each person manages their own sources and receives reports at their account email.</p>
    {error ? <p role="alert" className="mt-5 rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-800">{error}</p> : null}
    {notice ? <p role="status" className="mt-5 rounded-lg bg-[#eaf1e9] p-3 text-sm">{notice}</p> : null}
    <form className="mt-7 rounded-xl border border-[#e5eae4] bg-white p-5" onSubmit={async event => {
      event.preventDefault()
      if (await mutate("/auth/users", "POST", { email, password }, "Account created. Share the login details privately with this person.")) { setEmail(""); setPassword("") }
    }}>
      <h2 className="font-semibold">Add user</h2>
      <div className="mt-4 grid gap-4 sm:grid-cols-2">
        <label className="text-sm">Email<Input aria-label="Email" type="email" autoComplete="off" required maxLength={320} value={email} onChange={event => setEmail(event.target.value)} className="mt-1" /></label>
        <label className="text-sm">Password<Input aria-label="Password" type="password" autoComplete="new-password" required minLength={12} maxLength={1024} value={password} onChange={event => setPassword(event.target.value)} className="mt-1" /><span className="mt-1 block text-xs text-[#758179]">At least 12 characters.</span></label>
      </div>
      <Button className="mt-4" type="submit" disabled={busy}>{busy ? "Saving…" : "Create user"}</Button>
    </form>
    <section className="mt-7 overflow-hidden rounded-xl border border-[#e5eae4] bg-white">
      <h2 className="border-b border-[#edf0ec] p-5 font-semibold">Accounts</h2>
      {loading ? <p role="status" className="p-5 text-sm">Loading users…</p> : accounts.length === 0 ? <div className="p-5"><Button variant="outline" disabled={busy} onClick={() => void load().catch(cause => setError(cause.message))}>Reload users</Button></div> : accounts.map(account => <div key={account.id} className="border-b border-[#edf0ec] p-5 last:border-0">
        <div className="flex flex-wrap items-center justify-between gap-3"><div><p className="break-all text-sm font-medium">{account.email}</p><p className="mt-1 text-xs text-[#758179]">{account.role === "ADMIN" ? "Administrator · Existing account" : account.active ? "Member · Active" : "Member · Disabled"}</p></div>
          {account.role === "MEMBER" ? <div className="flex gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={() => { setResetId(account.id); setResetPassword("") }}>Reset password</Button><Button variant="outline" size="sm" disabled={busy} onClick={() => void mutate(`/auth/users/${account.id}`, "PUT", { active: !account.active }, account.active ? "Account disabled. Access and email reports are stopped; sources and history are preserved." : "Account enabled.")}>{account.active ? "Disable" : "Enable"}</Button></div> : null}
        </div>
        {resetId === account.id ? <form className="mt-4 flex flex-wrap items-end gap-3" onSubmit={async event => {
          event.preventDefault()
          if (await mutate(`/auth/users/${account.id}`, "PUT", { password: resetPassword }, "Password updated. Previous sessions are signed out.")) { setResetId(null); setResetPassword("") }
        }}><label className="text-sm">New password<Input aria-label={`New password for ${account.email}`} type="password" autoComplete="new-password" required minLength={12} maxLength={1024} value={resetPassword} onChange={event => setResetPassword(event.target.value)} className="mt-1" /></label><Button type="submit" disabled={busy}>Save password</Button><Button type="button" variant="outline" disabled={busy} onClick={() => { setResetId(null); setResetPassword("") }}>Cancel</Button></form> : null}
      </div>)}
    </section>
  </main>
}
