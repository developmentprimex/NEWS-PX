"use client"

import * as React from "react"
import { useRouter } from "next/navigation"
import { ArrowRight, Newspaper } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { useAuth } from "@/lib/auth"
import Link from "next/link"

export default function LoginPage() {
  const router = useRouter()
  const { login, user } = useAuth()
  const [email, setEmail] = React.useState("")
  const [password, setPassword] = React.useState("")
  const [error, setError] = React.useState("")
  const [busy, setBusy] = React.useState(false)

  React.useEffect(() => { if (user) router.replace("/intelligence") }, [user, router])

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setBusy(true)
    setError("")
    try { await login(email, password); router.replace("/intelligence") }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Sign in failed") }
    finally { setBusy(false) }
  }

  return <main className="flex min-h-screen items-center justify-center bg-[#f7f9f6] px-4"><div className="w-full max-w-sm rounded-2xl border border-[#e3e9e2] bg-white p-8 shadow-[0_18px_70px_rgba(25,40,30,0.05)]"><div className="flex size-11 items-center justify-center rounded-xl bg-[#e9f1e9] text-[#45684d]"><Newspaper className="size-5" /></div><h1 className="mt-6 text-2xl font-semibold tracking-tight text-[#1e2d22]">News Intelligence</h1><p className="mt-2 text-sm text-[#7b8b7e]">Sign in to your independent workspace.</p><form onSubmit={submit} className="mt-8 space-y-4"><div><label htmlFor="email" className="text-sm font-medium">Email</label><Input id="email" type="email" autoComplete="username" value={email} onChange={(event) => setEmail(event.target.value)} required className="mt-1.5" /></div><div><label htmlFor="password" className="text-sm font-medium">Password</label><Input id="password" type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} required className="mt-1.5" /></div>{error ? <p role="alert" className="text-sm text-red-700">{error}</p> : null}<Button type="submit" disabled={busy} className="mt-2 w-full">{busy ? "Signing in…" : "Sign in"}<ArrowRight className="size-4" /></Button></form><p className="mt-6 text-center text-xs text-[#617368]"><Link href="/privacy" className="underline underline-offset-4 hover:text-[#263d30]">Privacy Policy</Link></p></div></main>
}
