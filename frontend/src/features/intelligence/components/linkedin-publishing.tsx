"use client"

import * as React from "react"
import { ArrowUpRight, Linkedin, LoaderCircle, Repeat2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import type { NewsEntry } from "../types"

type ApiFetch = (path: string, init?: RequestInit) => Promise<Response>
type Connection = { id: string; authorUrn: string; name: string; kind: "PERSONAL" | "COMPANY"; expiresAt: string; connected: boolean; usable: boolean }
type Connections = { configured: boolean; callbackUrl: string; companyEnabled: boolean; connections: Connection[] }
type Receipt = { id: string; authorUrn: string; parentUrn: string; status: "publishing" | "published" | "failed" | "unknown"; message: string | null; url: string | null; commentary: string; approvedAt: string; publishedAt: string | null }
type PostStatus = { parentUrn: string | null; receipts: Receipt[] }

async function apiJson<T>(apiFetch: ApiFetch, path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init)
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "LinkedIn request failed. Please try again.")
  return payload as T
}

const jsonPost = (body: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })

function nativeUrn(value: string): string | null {
  const trimmed = value.trim()
  if (/^urn:li:(share|ugcPost):\d{1,30}$/.test(trimmed)) return trimmed
  try {
    const url = new URL(trimmed)
    if (url.protocol !== "https:" || !["linkedin.com", "www.linkedin.com"].includes(url.hostname) || url.username) return null
    return decodeURIComponent(url.pathname).match(/^\/feed\/update\/(urn:li:(?:share|ugcPost):\d{1,30})\/?$/)?.[1] ?? null
  } catch { return null }
}

export function LinkedInPublishingPanel({ apiFetch, compact = false }: { apiFetch: ApiFetch; compact?: boolean }) {
  const [data, setData] = React.useState<Connections | null>(null)
  const [error, setError] = React.useState("")
  const [busy, setBusy] = React.useState(false)
  const [organizationId, setOrganizationId] = React.useState("")
  const [manage, setManage] = React.useState(false)
  const load = React.useCallback(async () => {
    try { setData(await apiJson<Connections>(apiFetch, "/intelligence/linkedin/connections")); setError("") }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Connections could not be loaded.") }
  }, [apiFetch])
  React.useEffect(() => { void load() }, [load])
  React.useEffect(() => {
    const handler = () => { void load() }
    window.addEventListener("linkedin-connections-changed", handler)
    return () => window.removeEventListener("linkedin-connections-changed", handler)
  }, [load])
  const connect = async (company: boolean) => {
    setBusy(true); setError("")
    try {
      const result = await apiJson<{ authorizationUrl: string }>(apiFetch, "/intelligence/linkedin/connect", jsonPost(company ? { organization_id: organizationId.trim() } : {}))
      window.location.assign(result.authorizationUrl)
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Connection failed."); setBusy(false) }
  }
  const disconnect = async (id: string) => {
    setBusy(true); setError("")
    try {
      await apiJson(apiFetch, `/intelligence/linkedin/connections/${id}`, { method: "DELETE" })
      await load()
      window.dispatchEvent(new Event("linkedin-connections-changed"))
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Disconnect failed.") }
    finally { setBusy(false) }
  }
  const result = typeof window === "undefined" ? null : new URLSearchParams(window.location.search).get("linkedin")
  return <section aria-label="LinkedIn publishing accounts" className={compact ? "rounded-lg border border-[#e1e8e1] p-4" : "mt-5 rounded-xl border border-[#e1e8e1] bg-white p-5"}>
    <div className="flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="flex items-center gap-2 text-sm font-semibold text-[#214a31]"><Linkedin className="size-4 text-[#0a66c2]" /> Repost from this website</h2><p className="mt-1 text-xs leading-5 text-[#68776b]">Connect a publishing account, then approve a repost on any LinkedIn post.</p></div>
      {data?.configured ? <Button variant="outline" size="sm" disabled={busy} onClick={() => setManage(!manage)}>{manage ? "Hide accounts" : "Manage LinkedIn accounts"}</Button> : null}
    </div>
    {result === "connected" ? <p role="status" className="mt-3 text-sm text-[#346b45]">LinkedIn connected. Choose a post below to review and repost.</p> : result === "denied" ? <p role="status" className="mt-3 text-sm text-[#68776b]">LinkedIn connection was cancelled. You can connect again.</p> : result === "error" ? <p role="alert" className="mt-3 text-sm text-[#9a6e61]">LinkedIn connection failed. Check the app products, redirect URL, permissions and your Page role, then connect again.</p> : null}
    {error ? <p role="alert" className="mt-3 text-sm text-[#9a6e61]">{error} <button type="button" onClick={() => void load()} className="underline">Reload connections</button></p> : null}
    {!data && !error ? <p role="status" className="mt-3 text-xs text-[#68776b]">Loading publishing accounts…</p> : null}
    {data && !data.configured ? <div className="mt-3 text-xs leading-5 text-[#68776b]"><p>Publishing is waiting for LinkedIn app credentials and the encryption key to be configured on the server.</p><p className="mt-1">Register this redirect URL in the LinkedIn app:</p><code className="mt-1 block break-all rounded bg-[#f6f8f5] p-2">{data.callbackUrl}</code><a href="https://www.linkedin.com/developers/apps" target="_blank" rel="noopener noreferrer" className="mt-2 inline-block text-[#0a66c2] underline">Open LinkedIn Developer Portal ↗</a></div> : null}
    {data?.configured && (manage || !data.connections.some(row => row.usable)) ? <div className="mt-4 space-y-3">
      {data.connections.map(row => <div key={row.id} className="flex flex-wrap items-center justify-between gap-2 rounded-lg bg-[#f6f8f5] px-3 py-2"><div><p className="text-sm font-medium">{row.name} <span className="text-xs font-normal text-[#68776b]">· {row.kind === "COMPANY" ? "Company Page" : "Personal profile"}</span></p><p className="text-xs text-[#68776b]">{!row.connected ? "Disconnected" : row.usable ? `Authorization expires ${new Date(row.expiresAt).toLocaleDateString("en-GB")}` : "Authorization expired — reconnect this account"}</p></div>{row.connected ? <Button variant="ghost" size="sm" disabled={busy} onClick={() => void disconnect(row.id)}>Disconnect</Button> : null}</div>)}
      <Button variant="outline" size="sm" disabled={busy} onClick={() => void connect(false)}>{busy ? <LoaderCircle className="size-4 animate-spin" /> : <Linkedin className="size-4" />} Connect personal LinkedIn</Button>
      {data.companyEnabled ? <div className="flex flex-wrap items-end gap-2"><label className="min-w-0 flex-1 text-xs text-[#68776b]">LinkedIn company numeric ID<Input value={organizationId} onChange={event => setOrganizationId(event.target.value)} inputMode="numeric" placeholder="e.g. 12345678" className="mt-1" /></label><Button variant="outline" size="sm" disabled={busy || !/^\d{1,30}$/.test(organizationId.trim())} onClick={() => void connect(true)}>Connect company Page</Button><p className="w-full text-xs leading-5 text-[#68776b]">Use the Page ID from your LinkedIn Page admin URL. LinkedIn must confirm your permission to publish for that Page.</p></div> : <p className="text-xs leading-5 text-[#68776b]">Company Pages need approved Community Management permissions and company publishing enabled on the server.</p>}
      <p className="text-xs leading-5 text-[#68776b]">Disconnect removes this website’s saved credential. You can also revoke access in LinkedIn’s permitted services settings.</p>
    </div> : null}
  </section>
}

export function LinkedInRepostAction({ item, apiFetch }: { item: NewsEntry; apiFetch: ApiFetch }) {
  const [open, setOpen] = React.useState(false)
  const [data, setData] = React.useState<Connections | null>(null)
  const [post, setPost] = React.useState<PostStatus | null>(null)
  const [connectionId, setConnectionId] = React.useState("")
  const [commentary, setCommentary] = React.useState("")
  const [manualLink, setManualLink] = React.useState("")
  const [loading, setLoading] = React.useState(false)
  const [busy, setBusy] = React.useState(false)
  const [error, setError] = React.useState("")
  const [lastReceipt, setLastReceipt] = React.useState<Receipt | null>(null)
  const [reload, setReload] = React.useState(0)
  React.useEffect(() => {
    if (!open) return
    let active = true
    setLoading(true); setError(""); setPost(null); setData(null)
    const load = async () => {
      try {
        const [accounts, status] = await Promise.all([
          apiJson<Connections>(apiFetch, "/intelligence/linkedin/connections"),
          apiJson<PostStatus>(apiFetch, `/intelligence/items/${item.id}/repost`),
        ])
        if (!active) return
        setData(accounts)
        setConnectionId(current => accounts.connections.some(row => row.id === current && row.usable) ? current : accounts.connections.find(row => row.usable)?.id ?? "")
        setPost(status)
        if (!status.parentUrn && accounts.configured) {
          const prepared = await apiJson<PostStatus>(apiFetch, `/intelligence/items/${item.id}/repost/prepare`, jsonPost({}))
          if (active) setPost(prepared)
        }
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : "Post could not be prepared.") }
      finally { if (active) setLoading(false) }
    }
    void load()
    return () => { active = false }
  }, [open, item.id, apiFetch, reload])
  React.useEffect(() => {
    const handler = () => setReload(value => value + 1)
    window.addEventListener("linkedin-connections-changed", handler)
    return () => window.removeEventListener("linkedin-connections-changed", handler)
  }, [])
  const selected = data?.connections.find(row => row.id === connectionId)
  const parent = post?.parentUrn ?? nativeUrn(manualLink)
  const receipts = post?.receipts ?? []
  const receipt = receipts.find(row => row.authorUrn === selected?.authorUrn && row.parentUrn === parent)
    ?? receipts.find(row => row.authorUrn === selected?.authorUrn && !parent)
  const blocked = receipt && receipt.status !== "failed"
  const approve = async () => {
    if (busy || !selected?.usable || !parent || blocked || loading || error) return
    setBusy(true); setError("")
    try {
      const result = await apiJson<Receipt>(apiFetch, `/intelligence/items/${item.id}/repost`, jsonPost({ connection_id: selected.id, parent_urn: parent, commentary, approved: true }))
      setLastReceipt(result)
      setPost(current => ({ parentUrn: current?.parentUrn ?? parent, receipts: [result, ...(current?.receipts ?? []).filter(row => row.id !== result.id)] }))
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "No confirmation received. Reload the repost status before approving again.")
      // A lost web response may hide a successful publish. Refresh is required.
    } finally { setBusy(false) }
  }
  return <>
    <Button variant="ghost" size="sm" className="h-7 gap-1 px-2 text-xs text-[#0a66c2]" onClick={() => setOpen(true)}><Repeat2 className="size-3.5" />{lastReceipt?.status === "published" ? "Reposted" : "Review & repost"}</Button>
    <Dialog open={open} onOpenChange={value => { if (!busy) setOpen(value) }}>
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-xl" showCloseButton={!busy}>
        <DialogHeader><DialogTitle>Approve a LinkedIn repost</DialogTitle><DialogDescription>Review the post and publishing account. Approval publishes a public repost to LinkedIn.</DialogDescription></DialogHeader>
        <div className="rounded-lg bg-[#f6f8f5] p-3"><p className="text-xs text-[#68776b]">{item.sourceName}</p><p className="mt-1 text-sm font-semibold">{item.title}</p><p className="mt-2 line-clamp-4 whitespace-pre-wrap text-sm text-[#68776b]">{item.originalText || item.analysis.summary}</p>{item.url ? <a href={item.url} target="_blank" rel="noopener noreferrer" className="mt-2 inline-flex items-center gap-1 text-xs text-[#0a66c2] underline">Review source post <ArrowUpRight className="size-3" /></a> : null}</div>
        {loading ? <p role="status" className="flex items-center gap-2 text-sm text-[#68776b]"><LoaderCircle className="size-4 animate-spin" /> Preparing repost…</p> : null}
        {error ? <div role="alert" className="text-sm text-[#9a6e61]"><p>{error}</p><Button variant="outline" size="sm" className="mt-2" disabled={busy || loading} onClick={() => setReload(value => value + 1)}>Reload repost status</Button></div> : null}
        {!loading && data && (!data.configured || !data.connections.some(row => row.usable)) ? <LinkedInPublishingPanel apiFetch={apiFetch} compact /> : null}
        {data?.configured && data.connections.some(row => row.usable) ? <>
          <label className="text-sm font-medium">Repost as<select aria-label="Repost as" value={connectionId} disabled={busy || loading} onChange={event => setConnectionId(event.target.value)} className="mt-2 block w-full rounded-lg border border-[#dce4df] bg-white px-3 py-2 text-sm">{data.connections.map(row => <option key={row.id} value={row.id} disabled={!row.usable}>{row.name} · {row.kind === "COMPANY" ? "Company Page" : "Personal profile"}{!row.usable ? " (reconnect required)" : ""}</option>)}</select></label>
          {!loading && !post?.parentUrn && !receipt ? <div className="rounded-lg border border-[#e4d8bb] bg-[#fffaf0] p-3 text-xs leading-5 text-[#756440]"><p>The collected link identifies a LinkedIn activity. LinkedIn has not exposed the exact post identifier needed for an API repost.</p><label className="mt-2 block font-medium">Exact LinkedIn post link or identifier<Input aria-label="Exact LinkedIn post link or identifier" value={manualLink} disabled={busy} onChange={event => setManualLink(event.target.value)} placeholder="https://www.linkedin.com/feed/update/urn:li:ugcPost:…/" className="mt-1 bg-white" /></label><p className="mt-2">Use a link containing “urn:li:share:” or “urn:li:ugcPost:”. If you cannot obtain it, use the source link to repost on LinkedIn.</p></div> : null}
          {parent ? <a href={`https://www.linkedin.com/feed/update/${parent}/`} target="_blank" rel="noopener noreferrer" className="text-xs text-[#0a66c2] underline">Check the exact post that will be reposted ↗</a> : null}
          <label className="text-sm font-medium">Your text (optional)<textarea aria-label="Your repost text" value={commentary} maxLength={3000} disabled={busy || Boolean(blocked)} onChange={event => setCommentary(event.target.value)} placeholder="Add your thoughts, or leave empty for a repost without text." rows={3} className="mt-2 block w-full resize-y rounded-lg border border-[#dce4df] px-3 py-2 text-sm font-normal" /><span className="mt-1 block text-right text-xs font-normal text-[#87958a]">{commentary.length}/3000</span></label>
          {receipt ? <div role="status" className="rounded-lg bg-[#f6f8f5] p-3 text-sm"><p>{receipt.status === "published" ? "Reposted on LinkedIn." : receipt.status === "publishing" ? "A publish request was recorded. Check your LinkedIn feed; another request is blocked to prevent a duplicate." : receipt.message}</p>{receipt.url ? <a href={receipt.url} target="_blank" rel="noopener noreferrer" className="mt-2 inline-block text-[#0a66c2] underline">View your repost ↗</a> : null}{receipt.status === "unknown" || receipt.status === "publishing" ? <Button variant="outline" size="sm" className="mt-2" disabled={busy || loading} onClick={() => setReload(value => value + 1)}>Refresh status</Button> : null}</div> : null}
          <div className="flex flex-wrap justify-end gap-2"><Button variant="outline" disabled={busy} onClick={() => setOpen(false)}>Close</Button><Button className="bg-[#0a66c2] text-white hover:bg-[#08549f]" disabled={busy || loading || Boolean(error) || !selected?.usable || !parent || Boolean(blocked)} onClick={() => void approve()}>{busy ? <LoaderCircle className="size-4 animate-spin" /> : <Repeat2 className="size-4" />}{busy ? "Publishing…" : receipt?.status === "published" ? "Already reposted" : receipt?.status === "failed" ? "Approve another attempt" : "Approve & repost"}</Button></div>
        </> : null}
      </DialogContent>
    </Dialog>
  </>
}
