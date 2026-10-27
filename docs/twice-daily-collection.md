# Twice-daily social collection

Production collection uses 07:00 and 21:00 Europe/Budapest when
`NEWS_SOCIAL_SCHEDULE_ENABLED=true` and `NEWS_SOCIAL_DAILY_FETCH=false`.
The timezone follows daylight-saving changes. Combined reports retain the same
times. Website/RSS collection retains its periodic checks and report-slot reads.

Both report slots send a confirmation even when no new activities are available,
as long as at least one active source has email enabled. A zero-activity email
shows the interval and collection status. Partial coverage, failed or blocked
sources, pending results and sources absent from the cycle are distinguished
from completed checks with confirmed coverage. Zero results alone do not prove
that a source published nothing. The existing durable SMTP claim prevents
duplicate submissions, including for these empty reports. Previously recorded
empty slots are not replayed after an upgrade.

Each source starts at its last verified checkpoint. After a successful evening
cycle, morning collection covers `(21:00, 07:00]`; after a successful morning
cycle, evening covers `(07:00, 21:00]`. On a switch from closed UTC days, the first
cycle catches up from that source's existing checkpoint. Unresolved earlier
intervals remain anchored when no successful checkpoint exists. Failed, partial
or uncertain work must not silently move coverage forward.

Facebook requests exclude known stored post IDs in both slots. Exact timestamp
filtering and storage identity checks still apply after provider retrieval.
LinkedIn's date discovery may return overlapping records; two checks do not
guarantee equal credits or complete repost coverage. Unverified activity times
remain excluded from social email, and late verified items retain one-time
delivery receipts.

Adaptive record caps remain unchanged: small initial requests and bounded growth
from observed results. The shared operational monthly ceiling stays 4,000 records,
with a protected 1,000 reserve. The configured daily target remains flexible;
neither slot receives a separate monthly pool. Existing source pauses, provider
identity checks, billing proof requirements and the 30-minute request-start grace
remain enforced. Manual feed refreshes cannot trigger extra social collection.

Regression coverage includes transition from a completed daily checkpoint,
retention of unresolved intervals, exact cutoff ownership, deduplication,
Facebook exclusion inputs in both slots, daylight saving, restart/concurrency,
and budget denial. Provider and SMTP calls are mocked in tests.
