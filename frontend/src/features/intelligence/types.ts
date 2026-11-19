export type NewsCategory = "GRANT" | "TENDER" | "BUSINESS" | "TECHNOLOGY" | "EVENT" | "REGULATION" | "PARTNERSHIP" | "NEWS"
export type NewsPriority = "HIGH" | "MEDIUM" | "NORMAL"
export type IntelligenceView = "overview" | "for-you" | "repost-watch" | "primex-linkedin" | "news" | "opportunities" | "saved"
export type RepostGroup = "CEO" | "COMPANY"
export type NewsFilter = "For You" | "All" | "Grants" | "Tenders" | "Business" | "AI & Tech" | "Kosovo" | "EU" | "Events"

export interface NewsAnalysis {
  summary: string
  category: NewsCategory
  importanceScore: number
  relevanceScore: number
  whyItMatters: string | null
  deadline: string | null
  fundingAmount: string | null
  eligibility: string | null
  opportunityType: string | null
  tags: string[]
}

export interface NewsEntry {
  id: string
  sourceId: string
  sourceName: string
  sourceType: string
  externalId: string | null
  // The original article/call URL. Demo-only illustrative entries may have none.
  url: string | null
  title: string
  originalText: string | null
  publishedAt: string
  publicationTimeKnown?: boolean
  imageUrl: string | null
  contentHash: string | null
  createdAt: string
  readAt?: string | null
  emailedAt?: string | null
  savedAt?: string | null
  location: string | null
  priority: NewsPriority
  focusScore?: number
  linkedin?: LinkedInActivity | null
  repostGroups?: RepostGroup[]
  repostGuidance?: Partial<Record<RepostGroup, string>>
  analysis: NewsAnalysis
}

export interface LinkedInActivity {
  kind: "POST" | "REPOST" | "ARTICLE"
  authorName: string | null
  authorUrl: string | null
  reactionCount: number | null
  commentCount: number | null
  repostCount: number | null
  comments: { author: string | null; text: string; url: string | null; publishedAt: string | null }[]
  originalPostText: string | null
  originalPostUrl: string | null
  originalAuthor: string | null
  checkedAt: string | null
}

export interface DailyBrief {
  generatedAt: string
  headline: string
  itemIds: string[]
  closingNote: string
}

export interface DeepInsight {
  itemId: string
  generatedAt: string
  keyPoints: string[]
  businessImpact: string
  recommendedActions: string[]
  openQuestions: string[]
  confidence: "high" | "medium" | "low"
  evidenceLimitations: string | null
}

export interface StrategicBrief {
  generatedAt: string
  headline: string
  overview: string
  signals: { itemId: string; title: string; url: string; insight: string; nextStep: string }[]
  watchouts: string[]
}

export type SourceType = "WEBSITE" | "RSS" | "LINKEDIN" | "FACEBOOK" | "API" | "OTHER"
export type SourceStatus = "ACTIVE" | "PAUSED"
export type SourcePriority = "HIGH" | "NORMAL" | "LOW"

export interface NewsSource {
  id: string
  name: string
  url: string
  type: SourceType
  status: SourceStatus
  priority: SourcePriority
  categories: string[]
  ai_instructions: string | null
  fetch_interval_minutes: number
  last_checked_at: string | null
  last_started_at: string | null
  pending_snapshot_id: string | null
  last_error: string | null
  collection_supported: boolean
  collection_enabled?: boolean
  collection_note?: string | null
  email_enabled: boolean
  repost_groups: RepostGroup[]
  repost_guidance: Partial<Record<RepostGroup, string>>
  created_at: string
  updated_at: string
}

export type NewsSourceInput = Pick<NewsSource, "name" | "url" | "type" | "status" | "priority" | "categories" | "ai_instructions" | "fetch_interval_minutes" | "email_enabled" | "repost_groups" | "repost_guidance">

export interface DigestSettings {
  socialDailyFetch?: boolean
  socialFetchSchedule?: string
  scheduleManaged?: boolean
  times: string[]
  timezone: string
  recipient: string
  lastSentAt: string | null
  lastError: string | null
  emailConfigured: boolean
}
