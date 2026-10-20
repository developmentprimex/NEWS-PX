"use client"

import * as React from "react"
import Link from "next/link"
import { cn } from "@/lib/utils"
import { sourceCollectionStatus } from "../source-status"
import type { NewsSource, RepostGroup } from "../types"

export function RepostWatchPanel({ group, apiFetch }: { group: RepostGroup; apiFetch: (path: string) => Promise<Response> }) {
  const [sources, setSources] = React.useState<NewsSource[]>([])
  const [error, setError] = React.useState(false)
  React.useEffect(() => {
    let active = true
    const load = async () => {
      try {
        const response = await apiFetch("/intelligence/sources")
        if (!response.ok) throw new Error("Sources unavailable")
        const payload = await response.json() as NewsSource[]
        if (active) { setSources(payload); setError(false) }
      } catch { if (active) setError(true) }
    }
    void load()
    const timer = window.setInterval(() => void load(), 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [apiFetch])
  const selected = sources.filter((source) => source.repost_groups?.includes(group))
  return <div className="mt-7 rounded-xl border border-[#e5eae4] bg-white p-5">
    <nav aria-label="Repost owner" className="flex gap-2">{(["CEO", "COMPANY"] as const).map((target) => <Link key={target} href={`/intelligence/primex-linkedin?tab=repost-watch&group=${target}`} aria-current={group === target ? "page" : undefined} className={cn("rounded-lg px-4 py-2 text-sm font-medium", group === target ? "bg-[#eaf1e9] text-[#214a31]" : "text-[#64746a] hover:bg-[#f5f7f3]")}>{target === "CEO" ? "CEO · Sheet 1" : "Company · Sheet 2"}<span className="ml-2 text-xs">{sources.filter((source) => source.repost_groups?.includes(target)).length}</span></Link>)}</nav>
    <p className="mt-3 text-sm text-[#758179]">{group === "CEO" ? "Posts for your CEO to review and repost." : "Posts for your company page to review and repost."} Use Unread to find posts you have not reviewed. This page refreshes every 30 seconds.</p>
    <details className="mt-4 text-xs text-[#68776b]"><summary className="cursor-pointer font-medium">Monitored accounts ({selected.length})</summary>{error ? <p className="mt-3">Source status could not be refreshed.</p> : null}<div className="mt-3 grid gap-2 sm:grid-cols-2">{selected.map((source) => { const presentation = sourceCollectionStatus(source); return <div key={source.id} className="flex items-center justify-between gap-3 rounded-lg bg-[#f6f8f5] px-3 py-2"><a href={source.url} target="_blank" rel="noopener noreferrer" className="font-medium hover:underline">{source.name}</a><span className={presentation.tone === "error" ? "text-[#9a6e61]" : "text-[#91a096]"} title={presentation.message || undefined}>{presentation.tone === "normal" && source.last_checked_at ? `Every ${source.fetch_interval_minutes} min` : presentation.label}</span></div> })}</div><Link href="/intelligence/sources" className="mt-3 inline-block font-medium hover:underline">Manage sources →</Link></details>
  </div>
}
