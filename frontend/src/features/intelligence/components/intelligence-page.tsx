"use client"

import * as React from "react"
import Link from "next/link"
import { useRouter, useSearchParams } from "next/navigation"
import { toast } from "sonner"
import { AlertCircle, CalendarDays, RefreshCw, Search, SlidersHorizontal, Sparkles } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { useAuth } from "@/lib/auth"
import type { User } from "@/lib/types"
import { cn } from "@/lib/utils"
import { AIBrief } from "./ai-brief"
import { NewsCard } from "./news-card"
import { LinkedInPublishingPanel } from "./linkedin-publishing"
import { StrategicBriefPanel } from "./strategic-brief"
import { sourceCollectionStatus } from "../source-status"
import { filters } from "./news-filters"
import { intelligenceFeedService, type ReadState } from "../data/news.service"
import { linkedInSectionSources, selectLinkedInSources, sourceFilterKey, feedScopes, type LinkedInMonitoringSource } from "../linkedin-monitoring"
import { startCollectionCheck, loadCollectionStatus, type SourceCheckStatus } from "../collection-check"
import type { DailyBrief, IntelligenceView, NewsEntry, NewsFilter, NewsSource, StrategicBrief } from "../types"

const DEMO_READ_PREFIX = "news-intelligence-demo-read:"
const DEMO_SAVED_PREFIX = "news-intelligence-demo-saved:"
const opportunityCategories = new Set(["GRANT", "TENDER", "BUSINESS", "PARTNERSHIP"])
type FeedMode = ReadState | "dueSoon" | "saved"

function matchFilter(item: NewsEntry, filter: NewsFilter) {
  const { category, relevanceScore, tags } = item.analysis
  switch (filter) {
    case "For You": return category === "GRANT" || category === "TENDER" ? (item.focusScore ?? 0) >= 100 : relevanceScore >= 70
    case "All": return true
    case "Grants": return category === "GRANT"
    case "Tenders": return category === "TENDER"
    case "Business": return opportunityCategories.has(category)
    case "AI & Tech": return category === "TECHNOLOGY" || tags.includes("AI")
    case "Kosovo": return tags.includes("Kosovo")
    case "EU": return tags.includes("EU")
    case "Events": return category === "EVENT"
  }
}

function hasNearApplicationDeadline(item: NewsEntry, today: Date): boolean {
  if (!opportunityCategories.has(item.analysis.category) || !item.analysis.deadline) return false
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const end = new Date(start)
  end.setDate(end.getDate() + 31)
  const deadline = new Date(`${item.analysis.deadline}T12:00:00`)
  return deadline >= start && deadline < end
}

function FeedSkeleton() {
  return <div aria-label="Loading intelligence updates" role="status" className="space-y-3">{[0, 1, 2].map((index) => <div key={index} className="animate-pulse rounded-xl border border-[#e8eae7] bg-white p-6"><div className="h-3 w-24 rounded bg-[#e9eee9]"/><div className="mt-5 h-5 w-2/3 rounded bg-[#e9eee9]"/><div className="mt-3 h-3 w-full rounded bg-[#f0f2f0]"/><div className="mt-2 h-3 w-4/5 rounded bg-[#f0f2f0]"/><div className="mt-7 h-3 w-1/3 rounded bg-[#f0f2f0]"/></div>)}</div>
}

function FeedEmptyState({ view, feedMode, narrowed, priorityOnly, onReset }: { view: IntelligenceView; feedMode: FeedMode; narrowed: boolean; priorityOnly: boolean; onReset: () => void }) {
  const title = feedMode === "dueSoon" && !narrowed ? "No application deadlines in the next 30 days." : priorityOnly ? `No ${feedMode === "all" ? "" : `${feedMode} `}high priority updates.` : narrowed ? "No matching updates found." : view === "saved" || feedMode === "saved" ? "Nothing saved yet." : feedMode === "read" ? "Nothing read yet." : feedMode === "unread" ? "No unread updates." : "No updates yet."
  const description = feedMode === "dueSoon" && !narrowed ? "Grant, tender and business application deadlines will appear here when they are within 30 days." : priorityOnly ? feedMode === "read" ? "High priority updates you mark as read will appear here." : feedMode === "unread" ? "New high priority updates will appear here." : "High priority updates will appear here after the next source check." : narrowed ? "Try adjusting your filters or search." : view === "saved" || feedMode === "saved" ? "Bookmark an update to keep it here." : feedMode === "read" ? "Updates you open or mark as read will appear here." : "New updates will appear here after the next source check."
  return <div className="rounded-xl border border-dashed border-[#dce5dc] bg-white px-6 py-12 text-center"><SlidersHorizontal className="mx-auto size-6 text-[#a1b1a3]" /><h3 className="mt-3 font-semibold">{title}</h3><p className="mt-1 text-sm text-[#839087]">{description}</p>{narrowed ? <Button variant="ghost" size="sm" className="mt-4 text-[#4d7457]" onClick={onReset}>Clear filters</Button> : null}</div>
}

export function IntelligencePage({ view }: { view: IntelligenceView }) {
  const { user, apiFetch } = useAuth()
  const searchParams = useSearchParams()
  const selectedView = view === "primex-linkedin" && searchParams.get("tab") === "repost-watch" ? "repost-watch" : view
  return <IntelligenceWorkspace key={selectedView} view={selectedView} user={user} apiFetch={apiFetch} />
}

export function IntelligenceWorkspace({ view, user, apiFetch }: { view: IntelligenceView; user: Pick<User, "id" | "role"> | null; apiFetch: (path: string, init?: RequestInit) => Promise<Response> }) {
  const router = useRouter()
  const searchParams = useSearchParams()
  const [items, setItems] = React.useState<NewsEntry[]>([])
  const [brief, setBrief] = React.useState<DailyBrief | null>(null)
  const [strategicBrief, setStrategicBrief] = React.useState<StrategicBrief | null>(null)
  const [generatingBrief, setGeneratingBrief] = React.useState(false)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState(false)
  const [isDemo, setIsDemo] = React.useState(false)
  const [emailConfigured, setEmailConfigured] = React.useState(false)
  const [emailRecipient, setEmailRecipient] = React.useState("")
  const [aiConfigured, setAiConfigured] = React.useState(false)
  const primexView = view === "primex-linkedin"
  const activityView = primexView || view === "repost-watch"
  const [companySources, setCompanySources] = React.useState<LinkedInMonitoringSource[]>([])
  const companyFilter = searchParams.get("company") || "all"
  const selectedSources = selectLinkedInSources(companySources, companyFilter)
  const accountOptions = [{ key: "all", name: "All accounts" },
    ...companySources.map(source => ({ key: sourceFilterKey(source)!, name: source.name }))]
    .filter((entry, index, all) => all.findIndex(other => other.key === entry.key) === index)
  const companyLabel = companyFilter === "all" ? primexView ? "Mendex, PrimexEU and GA" : "LISTA GA" : selectedSources[0]?.name || "Selected account"
  function accountHref(value: string) {
    const params = new URLSearchParams(searchParams.toString())
    if (value === "all") params.delete("company")
    else params.set("company", value)
    return `/intelligence/primex-linkedin${params.size ? `?${params}` : ""}`
  }
  const [collectionEnabled, setCollectionEnabled] = React.useState<boolean | null>(null)
  const [selectedActivityKind, setActivityKind] = React.useState<"ALL" | "POST" | "REPOST">("ALL")
  const activityKind = selectedActivityKind
  const category = searchParams.get("category")
  const highPriorityOnly = !activityView && searchParams.get("priority") === "high"
  const filter: NewsFilter = activityView ? "All" : filters.find((value) => value === category) || (view === "for-you" ? "For You" : "All")
  const showBrief = view === "overview" || view === "for-you"
  const feedRequest = React.useRef(0)
  const [query, setQuery] = React.useState("")
  const [savedIds, setSavedIds] = React.useState<string[]>([])
  const [demoReadIds, setDemoReadIds] = React.useState<string[]>([])
  const [feedMode, setFeedMode] = React.useState<FeedMode>("all")
  const [emailingId, setEmailingId] = React.useState<string | null>(null)
  const [checkStatus, setCheckStatus] = React.useState<SourceCheckStatus | null>(null)
  const [startingCheck, setStartingCheck] = React.useState(false)

  const loadFeed = React.useCallback(async (quiet = false) => {
    const request = ++feedRequest.current
    if (!quiet) { setLoading(true); setError(false) }
    try {
      let scopedSources: LinkedInMonitoringSource[] | undefined
      if (activityView) {
        const response = await apiFetch("/intelligence/linkedin-sources")
        if (!response.ok) throw new Error("Could not load LinkedIn sources.")
        const sources = await response.json() as LinkedInMonitoringSource[]
        const monitored = linkedInSectionSources(sources, primexView ? "accounts" : "lista-ga")
        if (request !== feedRequest.current) return
        setCompanySources(monitored)
        scopedSources = selectLinkedInSources(monitored, companyFilter)
        setCollectionEnabled(monitored.some(source => source.collection_enabled === true))
        if (!scopedSources.length) { setItems([]); setIsDemo(false); setError(false); return }
      }
      const readState = view === "saved" || feedMode === "dueSoon" || feedMode === "saved" ? "all" : feedMode
      const feeds = scopedSources ? await Promise.all(feedScopes(scopedSources, activityKind)
        .map(scope => intelligenceFeedService.getFeed(apiFetch, readState, false, feedMode === "saved", undefined, scope))) : null
      if (feeds && !feeds.length) { setItems([]); setIsDemo(false); setError(false); return }
      const feed = feeds ? { ...feeds[0], items: feeds.flatMap((entry) => entry.items) } : await intelligenceFeedService.getFeed(apiFetch, readState, feedMode === "dueSoon" && view !== "saved", view === "saved" || feedMode === "saved")
      if (request !== feedRequest.current) return
      setItems(feed.items)
      setError(false)
      if (feed.isDemo) {
        try { setSavedIds(JSON.parse(localStorage.getItem(`${DEMO_SAVED_PREFIX}${user?.id || "guest"}`) || "[]")) }
        catch { setSavedIds([]) }
      } else setSavedIds(feed.items.filter((item) => Boolean(item.savedAt)).map((item) => item.id))
      setBrief(feed.brief)
      setIsDemo(feed.isDemo)
      setEmailConfigured(feed.emailConfigured)
      setEmailRecipient(feed.emailRecipient)
      setAiConfigured(feed.aiConfigured)
    } catch { if (!quiet && request === feedRequest.current) setError(true) }
    finally { if (request === feedRequest.current) setLoading(false) }
  }, [apiFetch, feedMode, view, user, activityView, primexView, activityKind, companyFilter])
  React.useEffect(() => { void loadFeed() }, [loadFeed])
  React.useEffect(() => {
    if (!activityView) return
    const timer = window.setInterval(() => void loadFeed(true), 30000)
    return () => window.clearInterval(timer)
  }, [activityView, loadFeed])

  React.useEffect(() => {
    if (isDemo) return
    let active = true
    void apiFetch("/intelligence/sources/check-all/status")
      .then(async (response) => response.ok ? await response.json() as SourceCheckStatus : null)
      .then((status) => { if (active && status?.state === "running") setCheckStatus(status) })
      .catch(() => {})
    return () => { active = false }
  }, [apiFetch, isDemo])

  React.useEffect(() => {
    if (!user || isDemo) return
    let active = true
    void loadCollectionStatus(apiFetch).then((status) => {
      if (active && status.cycle_id) setCheckStatus(status)
    }).catch(() => {})
    return () => { active = false }
  }, [apiFetch, user, isDemo])

  React.useEffect(() => {
    if (checkStatus?.state !== "running") return
    let polling = false
    const timer = window.setInterval(() => {
      if (polling) return
      polling = true
      void apiFetch("/intelligence/sources/check-all/status")
        .then(async (response) => {
          if (!response.ok) throw new Error("Could not get source check status.")
          return await response.json() as SourceCheckStatus
        })
        .then((status) => {
          setCheckStatus(status)
          if (status.state === "finished") {
            void loadFeed()
            if (status.failed) toast.error(`${status.failed} source ${status.failed === 1 ? "check failed" : "checks failed"}. See Sources for details.`)
            else if (status.partial || status.blocked) toast.info("Check finished with incomplete results. See Sources for details.")
            else if (status.pending === status.total) toast.info("Sources were checked recently or are already being checked. Try again in a few minutes.")
            else toast.success(status.started ? "Source checks finished. LinkedIn results may arrive shortly." : "Source checks finished. Feed updated.")
          }
        })
        .catch(() => { setCheckStatus(null); toast.error("Could not get source check status. Refresh the page to try again.") })
        .finally(() => { polling = false })
    }, 2000)
    return () => window.clearInterval(timer)
  }, [apiFetch, checkStatus?.state, loadFeed])

  const checkForUpdates = async () => {
    if (startingCheck || checkStatus?.state === "running") return
    setStartingCheck(true)
    try {
      const status = await startCollectionCheck(apiFetch)
      setCheckStatus(status)
      if (status.state === "finished") {
        await loadFeed()
        toast.info("This check has already finished. Feed refreshed.")
      }
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : "Could not start source checks.")
    } finally { setStartingCheck(false) }
  }

  React.useEffect(() => {
    if (!showBrief || !aiConfigured || isDemo) return
    let active = true
    void apiFetch("/intelligence/brief")
      .then(async (response) => response.ok ? await response.json() as StrategicBrief | null : null)
      .then((result) => { if (active) setStrategicBrief(result) })
      .catch(() => {})
    return () => { active = false }
  }, [apiFetch, showBrief, aiConfigured, isDemo])

  const generateBrief = async () => {
    if (generatingBrief) return
    setGeneratingBrief(true)
    try {
      const response = await apiFetch("/intelligence/brief", { method: "POST" })
      const payload = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "Could not generate the briefing.")
      setStrategicBrief(payload as StrategicBrief)
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : "Could not generate the briefing.")
    } finally {
      setGeneratingBrief(false)
    }
  }

  React.useEffect(() => {
    if (!user?.id) return
    try { setDemoReadIds(JSON.parse(localStorage.getItem(`${DEMO_READ_PREFIX}${user.id}`) || "[]")) } catch { setDemoReadIds([]) }
  }, [user?.id])

  const toggleSaved = (id: string) => {
    const wasSaved = savedIds.includes(id)
    setSavedIds((current) => {
      const next = current.includes(id) ? current.filter((value) => value !== id) : [...current, id]
      if (isDemo) localStorage.setItem(`${DEMO_SAVED_PREFIX}${user?.id || "guest"}`, JSON.stringify(next))
      return next
    })
    if (isDemo) return
    void (async () => {
      try {
        const response = await apiFetch(`/intelligence/items/${id}/saved`, { method: wasSaved ? "DELETE" : "PUT" })
        if (!response.ok) throw new Error("Could not update saved items")
      } catch {
        setSavedIds((current) => wasSaved ? [...new Set([...current, id])] : current.filter((value) => value !== id))
        toast.error("Could not update saved items. Please try again.")
      }
    })()
  }

  const setRead = (id: string, read: boolean) => {
    if (!user?.id) return
    if (isDemo) {
      setDemoReadIds((current) => {
        const next = read ? [...new Set([...current, id])] : current.filter((value) => value !== id)
        localStorage.setItem(`${DEMO_READ_PREFIX}${user.id}`, JSON.stringify(next))
        return next
      })
      return
    }
    const previous = items.find((item) => item.id === id)?.readAt || null
    setItems((current) => current.map((item) => item.id === id ? { ...item, readAt: read ? new Date().toISOString() : null } : item))
    void (async () => {
      try {
        const response = await apiFetch(`/intelligence/items/${id}/read`, { method: read ? "PUT" : "DELETE" })
        if (!response.ok) throw new Error("Could not update reading status.")
      } catch {
        setItems((current) => current.map((item) => item.id === id ? { ...item, readAt: previous } : item))
        toast.error("Could not update reading status. Please try again.")
      }
    })()
  }

  const sendEmail = async (id: string) => {
    if (isDemo || emailingId) return
    setEmailingId(id)
    try {
      const response = await apiFetch(`/intelligence/items/${id}/email`, { method: "POST" })
      const payload = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "Could not send this update.")
      setItems((current) => current.map((item) => item.id === id ? { ...item, emailedAt: payload.sentAt } : item))
      toast.success(payload.alreadySent ? "This update was already emailed." : `Sent to ${payload.recipient}`)
    } catch (cause) {
      toast.error(cause instanceof Error ? cause.message : "Could not send this update.")
    } finally {
      setEmailingId(null)
    }
  }

  const visibleItems = React.useMemo(() => {
    const needle = query.trim().toLowerCase()
    const today = new Date()
    const matching = items.filter((item) => {
      if ((view === "saved" || feedMode === "saved") && !savedIds.includes(item.id)) return false
      if (view !== "saved") {
        const isRead = isDemo ? demoReadIds.includes(item.id) : Boolean(item.readAt)
        if ((feedMode === "read" && !isRead) || (feedMode === "unread" && isRead)) return false
      }
      if (view === "opportunities" && !opportunityCategories.has(item.analysis.category)) return false
      if (activityView && !selectLinkedInSources(companySources, companyFilter).some(source => source.id === item.sourceId)) return false
      if (activityView && activityKind !== "ALL" && (activityKind === "REPOST" ? item.linkedin?.kind !== "REPOST" : item.linkedin?.kind === "REPOST")) return false
      if (feedMode === "dueSoon" && !hasNearApplicationDeadline(item, today)) return false
      if (highPriorityOnly && item.priority !== "HIGH") return false
      if (!((feedMode === "dueSoon" || feedMode === "saved") && filter === "For You") && !matchFilter(item, filter)) return false
      if (!needle) return true
      return [item.title, item.analysis.summary, item.sourceName, ...item.analysis.tags].some((value) => value.toLowerCase().includes(needle))
    })
    if (feedMode === "dueSoon") return matching.sort((a, b) => (a.analysis.deadline || "").localeCompare(b.analysis.deadline || ""))
    const newestFirst = (a: NewsEntry, b: NewsEntry) =>
      new Date(b.publishedAt || b.createdAt).getTime() - new Date(a.publishedAt || a.createdAt).getTime()
      || new Date(b.createdAt).getTime() - new Date(a.createdAt).getTime()
    return view === "for-you" || view === "opportunities"
      ? matching.sort((a, b) => (b.focusScore ?? 0) - (a.focusScore ?? 0) || newestFirst(a, b))
      : matching.sort(newestFirst)
  }, [items, view, savedIds, demoReadIds, isDemo, feedMode, highPriorityOnly, filter, query, activityView, companySources, companyFilter, activityKind])

  const opportunities = items.filter((item) => opportunityCategories.has(item.analysis.category)).length
  const today = new Date()
  const todayStart = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime()
  const upcoming = items.filter((item) => item.analysis.deadline && new Date(`${item.analysis.deadline}T12:00:00`).getTime() >= todayStart).sort((a, b) => (a.analysis.deadline || "").localeCompare(b.analysis.deadline || "")).slice(0, 3)
  const pageHeading = highPriorityOnly ? "High priority" : activityView ? "INT Linkedin Activity" : view === "overview" ? "Overview" : view === "for-you" ? "For You" : view === "news" ? "News" : view === "saved" ? "Saved" : "Opportunities"

  return <div className="min-h-screen px-4 pb-16 pt-7 text-[#24342a] sm:px-7 lg:px-10 lg:pt-10">
    <div className="mx-auto max-w-[1370px]">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div><div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.16em] text-[#83958a]"><span className="size-1.5 rounded-full bg-[#7ca889]" /> News portal <span className="font-normal normal-case tracking-normal text-[#a0aaa2]">/ {isDemo ? "Sample data" : "Live sources"}</span></div><h1 className="mt-2 text-[32px] font-semibold tracking-[-0.045em] sm:text-[38px]">{pageHeading}</h1><p className="mt-1 text-sm text-[#758179]">{primexView ? "Original posts and supported reposts from your LinkedIn sources." : view === "repost-watch" ? "Posts and reposts from all other LinkedIn accounts in LISTA GA." : view === "for-you" ? "Selected updates and opportunities relevant to your company." : view === "overview" ? "All updates and opportunities, newest first." : "Updates and opportunities, all in one place."}</p></div>
        {!isDemo ? <div className="flex flex-wrap gap-2"><Button size="sm" disabled={loading || (activityView && !selectedSources.some((source) => source.status === "ACTIVE")) || startingCheck || checkStatus?.state === "running"} className="bg-[#2d5b3d] text-white hover:bg-[#244b32]" onClick={() => void checkForUpdates()}><RefreshCw className={cn("size-4", (startingCheck || checkStatus?.state === "running") && "animate-spin")} /> {checkStatus?.state === "running" ? `Checking ${checkStatus.processed}/${checkStatus.total}…` : startingCheck ? "Starting…" : "Check for updates"}</Button></div> : null}
      </div>

      {activityView && !isDemo && user ? <LinkedInPublishingPanel apiFetch={apiFetch} /> : null}
      {activityView ? <nav aria-label="LinkedIn activity sections" className="flex flex-wrap gap-2 rounded-xl border border-[#e1e8e1] bg-white p-2">
        {[{ label: "Account activity", href: "/intelligence/primex-linkedin", active: primexView }, { label: "LISTA GA", href: "/intelligence/primex-linkedin?tab=repost-watch", active: view === "repost-watch" }].map((tab) => <Link key={tab.label} href={tab.href} aria-current={tab.active ? "page" : undefined} className={cn("rounded-lg px-4 py-2 text-sm font-medium", tab.active ? "bg-[#eaf1e9] text-[#214a31]" : "text-[#64746a] hover:bg-[#f5f7f3]")}>{tab.label}</Link>)}
      </nav> : null}
      {activityView ? <div className="mt-7 rounded-xl border border-[#e5eae4] bg-white p-5">
        <div className="flex flex-wrap items-center justify-between gap-3"><h2 className="font-semibold text-[#214a31]">LinkedIn activity</h2><Link href="/intelligence/sources" className="text-xs text-[#64746a] hover:underline">Manage sources</Link></div>
        <p className="mt-2 text-sm text-[#758179]">{primexView ? "Mendex, PrimexEU and GA, newest first." : "All other LinkedIn accounts, newest first."} Refreshing reads stored posts; it does not start collection.</p>
        <p role="status" className="mt-2 text-sm text-[#68776b]">{collectionEnabled === true ? "Collection is enabled subject to source eligibility, billing verification and available budget." : "Collection is paused or not enabled. Existing posts remain available."}</p>
        {primexView ? <div className="mt-4">
          <p className="mb-2 text-sm font-medium text-[#214a31]">Account</p>
          <nav aria-label="LinkedIn accounts" className="flex flex-wrap gap-2">
            {accountOptions.map(account => <Link key={account.key} href={accountHref(account.key)} aria-current={companyFilter === account.key ? "page" : undefined} className={cn("rounded-lg px-4 py-2 text-sm font-medium", companyFilter === account.key ? "bg-[#eaf1e9] text-[#214a31]" : "text-[#64746a] hover:bg-[#f5f7f3]")}>{account.name}</Link>)}
          </nav>
        </div> : <div className="mt-4 w-full sm:max-w-sm">
          <label htmlFor="linkedin-account-filter" className="mb-2 block text-sm font-medium text-[#214a31]">Account</label>
          <Select value={companyFilter} onValueChange={(value) => router.push(accountHref(value))}>
            <SelectTrigger id="linkedin-account-filter" className="w-full border-[#e5eae4] bg-white shadow-none">
              <SelectValue placeholder="Select an account" />
            </SelectTrigger>
            <SelectContent position="popper" align="start" className="max-h-80">
              {accountOptions.map(account => <SelectItem key={account.key} value={account.key}>{account.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>}
        {companyFilter !== "all" && selectedSources.length > 0 ? <div className="mt-4 space-y-3">{selectedSources.map(source => { const presentation = sourceCollectionStatus(source); return <div key={source.id} className="rounded-lg border border-[#e5eae4] p-3">
          <a href={source.url} target="_blank" rel="noopener noreferrer" className="text-sm font-semibold text-[#214a31] hover:underline">{source.name} on LinkedIn</a>
          <p className="mt-2 text-xs text-[#68776b]">{source.status === "PAUSED" ? "Monitoring paused" : source.pending_snapshot_id ? "Collection pending" : "Scheduled monitoring, subject to budget"}{source.last_checked_at ? ` · Last checked ${new Date(source.last_checked_at).toLocaleString("en-GB")}` : " · No completed check yet"}</p>
          {presentation.message ? <p role={presentation.tone === "error" ? "alert" : "status"} className="mt-2 text-xs text-[#9a6e61]">{presentation.message}</p> : null}
        </div> })}</div> : null}
        <div role="group" aria-label="LinkedIn activity type" className="mt-4 inline-flex gap-1 rounded-lg bg-[#edf1ec] p-1 text-sm">{(["ALL", "POST", "REPOST"] as const).map(kind => <button key={kind} type="button" aria-pressed={activityKind === kind} onClick={() => setActivityKind(kind)} className={cn("rounded-md px-4 py-1.5", activityKind === kind ? "bg-white font-medium text-[#214a31] shadow-sm" : "text-[#64746a]")}>{kind === "ALL" ? "All activity" : kind === "POST" ? "Posts" : "Reposts"}</button>)}</div>
      </div> : null}
      {showBrief && brief && !loading ? <div className="mt-8"><AIBrief brief={brief} items={items} /></div> : null}
      {showBrief && !loading && !isDemo && aiConfigured && items.length ? <div className="mt-8"><StrategicBriefPanel brief={strategicBrief} generating={generatingBrief} onGenerate={() => void generateBrief()} /></div> : null}

      <div className={cn("mt-8 grid gap-9", !activityView && "xl:grid-cols-[minmax(0,1fr)_280px] xl:gap-10")}>
        <section id="intelligence-feed" className="min-w-0">
          <div className="flex flex-wrap items-end justify-between gap-3"><div><div className="flex items-center gap-2 text-[11px] font-semibold uppercase tracking-[0.14em] text-[#83958a]"><span className="size-1.5 rounded-full bg-[#8ab397]" /> {activityView ? primexView ? "Company activity" : "LISTA GA" : highPriorityOnly ? "Priority feed" : filter === "All" ? "All sources" : filter}</div><h2 className="mt-1 text-xl font-semibold tracking-tight">{activityView ? (activityKind === "REPOST" ? `${companyLabel} reposts` : activityKind === "POST" ? `${companyLabel} posts` : `Latest ${companyLabel} activity`) : highPriorityOnly ? "High priority updates" : view === "saved" || feedMode === "saved" ? "Your saved updates" : feedMode === "dueSoon" ? "Application deadlines within 30 days" : feedMode === "read" ? "Read updates" : view === "opportunities" ? "Opportunity watch" : view === "for-you" ? "Relevant signals" : "Latest signals"}</h2></div><span className="text-xs text-[#8c978e]">{loading ? "Loading…" : `${visibleItems.length} ${visibleItems.length === 1 ? "update" : "updates"}`}</span></div>
          {view !== "saved" ? <div role="group" aria-label="Update filters" className="mt-4 inline-flex rounded-lg bg-[#edf1ec] p-0.5 text-xs font-medium">{(activityView ? (["all", "unread", "read", "saved"] as const) : (["all", "unread", "read", "dueSoon", "saved"] as const)).map((mode) => <button key={mode} type="button" aria-pressed={feedMode === mode} title={mode === "dueSoon" ? "Application deadlines in the next 30 days" : undefined} onClick={() => setFeedMode(mode)} className={cn("rounded-md px-3.5 py-1.5 transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#5e806c]", feedMode === mode ? "bg-white text-[#2c4533] shadow-sm" : "text-[#76857a] hover:text-[#2c4533]")}>{mode === "all" ? "All" : mode === "unread" ? "Unread" : mode === "read" ? "Read" : mode === "dueSoon" ? "Due soon" : activityView ? "Saved posts" : "Saved news"}</button>)}</div> : null}
          {feedMode === "dueSoon" && view !== "saved" ? <p className="mt-2 text-xs text-[#758179]">Grants, tenders and other applications closing within 30 days, including read updates.</p> : null}
          <div className="relative mt-5"><Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-[#9ca9a0]"/><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search topics, sources, or keywords" aria-label="Search intelligence updates" className="h-10 border-[#e4eae3] bg-white pl-9 shadow-none focus-visible:ring-[#acc7b1]/40" /></div>
          <div className="mt-5 space-y-3">{loading ? <FeedSkeleton /> : error ? <div role="alert" className="rounded-xl border border-[#e8eae7] bg-white px-6 py-10 text-center"><AlertCircle className="mx-auto size-6 text-[#89998b]" /><h3 className="mt-3 font-semibold">Updates could not be loaded.</h3><p className="mt-1 text-sm text-[#7f8a80]">Please refresh the page and try again.</p></div> : visibleItems.length ? visibleItems.map((item) => <NewsCard key={item.id} item={item} activityView={activityView} saved={savedIds.includes(item.id)} read={isDemo ? demoReadIds.includes(item.id) : Boolean(item.readAt)} canEmail={!isDemo && emailConfigured} emailRecipient={emailRecipient} canAnalyze={!isDemo && aiConfigured} canRepost={!isDemo && Boolean(user)} apiFetch={apiFetch} emailSending={emailingId === item.id} emailBusy={Boolean(emailingId)} onEmail={(id) => void sendEmail(id)} onToggleSaved={toggleSaved} onSetRead={setRead} />) : <FeedEmptyState view={view} feedMode={feedMode} priorityOnly={highPriorityOnly} narrowed={activityView && (companyFilter !== "all" || activityKind !== "ALL") || Boolean(query.trim()) || highPriorityOnly || filter !== "All" && !(view === "for-you" && filter === "For You")} onReset={() => { router.push(view === "overview" ? "/intelligence" : view === "for-you" ? "/intelligence/for-you?category=All" : view === "repost-watch" ? "/intelligence/primex-linkedin?tab=repost-watch" : "/intelligence/" + view); setQuery(""); setActivityKind("ALL") }} />}</div>
        </section>
        {!activityView ? <aside className="space-y-6 xl:pt-1" aria-label="Intelligence highlights">
          <div className="rounded-xl border border-[#e8ece7] bg-white p-5"><div className="flex items-center gap-2 text-[#5e8067]"><Sparkles className="size-4" /><span className="text-[11px] font-semibold uppercase tracking-[0.12em]">At a glance</span></div><div className="mt-5 grid grid-cols-2 gap-4"><Link href="/intelligence/news?priority=high" className="rounded-lg -m-2 p-2 transition-colors hover:bg-[#f2f6ef] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#54775d]" aria-label="View high priority updates"><div className="text-2xl font-semibold tracking-tight">{items.filter((item) => item.priority === "HIGH").length}</div><div className="mt-1 text-xs text-[#5e8067]">High priority →</div></Link><div><div className="text-2xl font-semibold tracking-tight">{opportunities}</div><div className="mt-1 text-xs text-[#89958a]">Opportunities</div></div></div><p className="mt-5 border-t border-[#eef1ed] pt-4 text-xs leading-5 text-[#91a096]">Priority favors current grants and tenders for a Kosovo AI company. Check eligibility at the source.</p></div>
          {upcoming.length ? <div className="px-1"><div className="flex items-center justify-between"><h3 className="flex items-center gap-2 text-sm font-semibold"><CalendarDays className="size-4 text-[#7f9b85]" /> Dates to watch</h3>{isDemo ? <span className="text-[11px] text-[#a0aaa2]">Sample</span> : null}</div><div className="mt-4 space-y-0">{upcoming.map((item) => <div key={item.id} className="flex items-start gap-3 border-b border-[#e8ece7] py-3"><span className="min-w-11 text-xs font-semibold text-[#5d7964]">{new Date(`${item.analysis.deadline}T12:00:00`).toLocaleDateString("en-GB", { day: "numeric", month: "short" })}</span><div className="min-w-0"><div className="text-xs font-medium leading-5 text-[#36473b]">{item.title}</div><div className="mt-0.5 text-[11px] text-[#9aa69c]">{item.sourceName}</div></div></div>)}</div></div> : null}
        </aside> : null}
      </div>
    </div>
  </div>
}
