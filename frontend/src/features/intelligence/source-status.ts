import type { NewsSource } from "./types"

type StatusSource = Pick<NewsSource, "type" | "status" | "last_error" | "pending_snapshot_id" | "last_checked_at"> & Partial<Pick<NewsSource, "collection_supported" | "collection_enabled" | "collection_note">>

export type CollectionPresentation = {
  label: string
  message: string | null
  tone: "normal" | "waiting" | "error"
}

function collectionIssue(source: StatusSource): CollectionPresentation | null {
  const error = source.last_error?.trim()
  if (!error) return null
  if (error === 'No current announcements found.') return {
    label: 'No current announcements', tone: 'waiting',
    message: 'The website was read successfully, but no current news articles or calls matched this source.',
  }
  if (error === 'Provider could not read the source; interval coverage remains unconfirmed.') return {
    label: 'Provider read incomplete', tone: 'waiting',
    message: 'The provider could not read this source. The next scheduled cycle can try again; coverage remains unconfirmed.',
  }
  if (error.startsWith('Record limit reached; interval remains unconfirmed.')) return {
    label: 'Partial coverage', tone: 'waiting',
    message: 'The request limit was reached. The next scheduled cycle can use a larger limit within the shared budget.',
  }
  if (error.startsWith('Snapshot contains invalid, unattributed or out-of-window activity;')) return {
    label: source.collection_enabled === false ? 'Review required' : 'Partial coverage',
    tone: source.collection_enabled === false ? 'error' : 'waiting',
    message: source.collection_enabled === false
      ? 'Collection is held until source identity and activity dates are verified. Stored posts remain available.'
      : 'Some returned activity could not be verified for this source and interval. The next scheduled cycle can check again.',
  }
  if (error.startsWith('This Facebook group is private;')) return {
    label: 'Private source', tone: 'error', message: 'This group is private. Choose a public source to collect its updates.',
  }
  if (error === 'Provider returned no results; interval coverage remains unconfirmed.') return {
    label: 'No results returned', tone: 'waiting',
    message: 'The provider returned no usable results. Coverage is unconfirmed; the next scheduled cycle can check again.',
  }
  if (error === 'Record limit reached; completeness is unknown. Authorize a separate backfill or reconcile the window.') return {
    label: 'Partial coverage', tone: 'waiting',
    message: 'The result limit was reached. The uncovered interval is retained; collection needs a larger request or reconciliation.',
  }
  if (error.startsWith('Billing observer stopped;')) return {
    label: 'Billing verification paused', tone: 'waiting',
    message: 'Collection is waiting for verified provider billing. Stored posts remain available.',
  }
  if (source.type === "LINKEDIN") {
    if (error === "Daily record target reached.") return {
      label: "Daily allowance reached", tone: "waiting",
      message: "The last LinkedIn check was deferred by the daily collection allowance. Automatic checks will retry when allowance is available.",
    }
    if (error === "Operational monthly record budget reached.") return {
      label: "Monthly allowance reached", tone: "waiting",
      message: "The last LinkedIn check was deferred by the monthly collection allowance. Automatic checks will retry when allowance is available.",
    }
  }
  if (source.type === "WEBSITE" || source.type === "RSS") {
    const status = Number(error.match(/(?:error\s+['"]|HTTP\s+)([45]\d{2})\b/i)?.[1])
    if (status === 403) return {
      label: "Access blocked", tone: "error",
      message: "This website refused automated access (HTTP 403). Use an accessible official feed or ask the website owner to allow access.",
    }
    if (status === 526 || /CERTIFICATE_VERIFY_FAILED|certificate verify failed|ERR_CERT/i.test(error)) return {
      label: "Website certificate error", tone: "error",
      message: `The website's HTTPS certificate could not be verified${status === 526 ? " (HTTP 526)" : ""}. Its owner needs to fix the certificate before collection can continue.`,
    }
    if (status >= 500 && status <= 599) return {
      label: "Website unavailable", tone: "error",
      message: `The website returned HTTP ${status}. Automatic checks will try again at the configured interval; persistent problems need attention from the website owner.`,
    }
  }
  return { label: "Check failed", message: error, tone: "error" }
}

export function sourceCollectionStatus(source: StatusSource, linkedinConfigured = true): CollectionPresentation {
  const issue = collectionIssue(source)
  if (source.status === "PAUSED") return { label: "Paused", message: issue?.message ?? null, tone: "waiting" }
  if (source.collection_supported === false) return { label: "No connector", message: issue?.message ?? source.collection_note ?? null, tone: "waiting" }
  if (source.type === "LINKEDIN" && !linkedinConfigured) return { label: "Setup needed", message: issue?.message ?? null, tone: "waiting" }
  if (source.pending_snapshot_id) return { label: "Checking", message: null, tone: "waiting" }
  if (source.collection_enabled === false && source.collection_note) return issue ?? {
    label: 'Setup needed', message: source.collection_note, tone: 'waiting',
  }
  if (source.type === "FACEBOOK" && source.collection_note) return { label: "Setup needed", message: source.collection_note, tone: "waiting" }
  if (source.collection_enabled === false) return issue ?? { label: 'Collection disabled', message: 'Collection is disabled for this source. Stored posts remain available.', tone: 'waiting' }
  return issue ?? { label: source.last_checked_at ? "Monitoring" : "Ready", message: null, tone: "normal" }
}
