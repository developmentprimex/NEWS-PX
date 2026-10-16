import type { NewsSource } from "./types"
import type { FeedScope } from "./data/news.service"

export type LinkedInMonitoringSource = Pick<NewsSource, "id" | "name" | "url" | "type" | "status" | "fetch_interval_minutes" | "last_checked_at" | "pending_snapshot_id" | "last_error" | "collection_enabled">

const legacyFilters: Record<string, string> = {
  "/company/primexeu": "primex", "/company/mendex-ai": "mendex",
  "/in/ganimete-arifaj-canolli-27641927a": "ganimete",
}

export function sourceFilterKey(source: LinkedInMonitoringSource): string | undefined {
  if (source.type !== "LINKEDIN") return undefined
  try {
    const url = new URL(source.url)
    if (!["http:", "https:"].includes(url.protocol) || !["linkedin.com", "www.linkedin.com"].includes(url.hostname) || url.port || url.username || url.password) return undefined
    const path = url.pathname.replace(/\/+$/, "").toLowerCase()
    if (!/^\/(in|company)\/[^/]+$/.test(path)) return undefined
    return legacyFilters[path] || source.id
  } catch { return undefined }
}

export function eligibleLinkedInSources(sources: LinkedInMonitoringSource[]): LinkedInMonitoringSource[] {
  // Paused sources retain their historical feed; only ACTIVE sources are collected by the backend.
  return sources.filter(source => Boolean(sourceFilterKey(source)))
}

export function selectLinkedInSources(sources: LinkedInMonitoringSource[], filter: string): LinkedInMonitoringSource[] {
  return eligibleLinkedInSources(sources).filter(source => filter === "all" || sourceFilterKey(source) === filter)
}

export function feedScopes(sources: LinkedInMonitoringSource[], activity: "ALL" | "POST" | "REPOST"): FeedScope[] {
  const scopes: FeedScope[] = []
  for (let i = 0; i < sources.length; i += 20) scopes.push({
    sourceIds: sources.slice(i, i + 20).map(source => source.id),
    activityKind: activity === "ALL" ? undefined : activity,
  })
  return scopes
}
