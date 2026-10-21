# Direct Opportunities for Youth monitoring

The existing official website source uses its public WordPress Posts API instead
of a paid social discovery endpoint. No language model or extra provider account
is needed to discover announcements.

The collector requests updates since its own durable checkpoint, with a two-hour
overlap to tolerate delayed visibility. Initial activation reads the preceding
48 hours. It follows every page in the filtered interval, validates stable IDs,
same-site links, GMT timestamps, total counts and pagination consistency, and
retains both new announcements and updates to known article URLs. Existing item
identities and personal read/saved state remain intact.

The checkpoint advances in the same transaction as item storage only after all
pages succeed. Access failures, malformed records, changed/truncated pagination
and more than 40 pages leave the interval unresolved. Up to three bounded retries
are allowed for transient GET failures. No provider POST or SMTP send is performed
by this connector. Configure the existing website source to run every 60 minutes.

The accepted source policy is official website announcements, including updates;
it does not claim to capture social-only posts, comments or repost events. Pause
only the two Opportunities for Youth social sources after verifying a complete
official website collection. Keep the distinct Youth Opportunities / youthop.com
sources and the owner's other source preferences unchanged.

For other tracked Facebook/LinkedIn sources, ingestion retains attributable
activity regardless of category selection. Category selection must not discard
already retrieved social posts. Invalid identities and unverifiable event times
retain their existing safeguards; daily reporting still excludes unverified
activity times. Adaptive caps grow from observed output and genuinely saturated
limits, without letting an unrelated large but unsaturated pilot limit inflate
quiet sources' reservations.

Daily social collection, durable unconfirmed intervals, billing verification and
the 4,000-record monthly ceiling remain in force. These controls cannot prove
that a third party returned every public activity or retrieve private/deleted
content. Incomplete intervals must stay visible instead of being certified as
complete. A once-daily schedule is an operating target, not a guarantee of
delivery within exactly 24 hours during outages or provider omissions.

Public contract: [WordPress Posts API](https://developer.wordpress.org/rest-api/reference/posts/).
