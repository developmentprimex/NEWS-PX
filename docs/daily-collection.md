# Daily collection and verified billing

Enable `NEWS_SOCIAL_DAILY_FETCH=true` to collect Facebook/LinkedIn once per closed
UTC day, starting at 00:05 UTC with a 30-minute startup window. UTC days are always
24 hours, including local daylight-saving transitions. Combined emails remain at
07:00/21:00 Europe/Budapest. Website/RSS sources join those two collection slots
and retain their ordinary periodic checks. The daily mode excludes unknown
publication times from reports. Verified late results remain deliverable once.

`NEWS_SOCIAL_ADAPTIVE_LIMITS=true` starts unknown sources at three records, learns
from up to seven observed snapshots, and increases saturated request caps up to
`NEWS_SOCIAL_MAX_RECORD_LIMIT` (at most 130). This is a maximum reservation, not
an assertion that quiet sources publish at most two posts. Saturation retains
the uncovered interval. Facebook daily requests send known post IDs through the
provider-supported `posts_to_not_include` input. The LinkedIn discovery contract
does not offer that exclusion field; duplicate discoveries may consume credits.

`NEWS_SOCIAL_FLEXIBLE_DAILY_BUDGET=true` shares the existing protected monthly pool
across Facebook/LinkedIn instead of imposing the legacy 130/day and Facebook
12/day, 300/month pilot sublimits. It never increases the 4000 operational ceiling,
the protected 1000 reserve, or the verified 5000 allowance. Collection stops at the
monthly ceiling. High-volume sources can exhaust this pool; neither once-daily
collection nor adaptive caps guarantee exhaustive coverage within free credits.

Billing proof includes exact dataset-to-meter bindings, per-day/per-month sums,
account identity and a rollup cutoff. Only completed executions bound to that
same account and dataset and strictly older than a fresh proof cutoff cease to
consume outstanding reservations. The ledger's original reservations remain
unchanged. Unknown-account executions, unfinished/ambiguous requests and work
completed after the cutoff stay reserved. Unsupported or inconsistent meters
fail closed. Historical account binding is an explicit audited operator repair.

Billing GETs retry transient transport/429/5xx failures at most three times.
Only a known transport-only observer pause can recover following a complete,
active, account-bound proof. Credit, manual, identity and ambiguous-trigger pauses
remain protected. Billing failures defer new social requests within the current
authorized window; they do not block website reads. Ambiguous POSTs never retry.

The operator `python -m app.collection_repair_admin` previews changes by default.
Applying requires `--apply --approval-ref`; historical `--bind-cycle` additionally
requires the independently reviewed `--account-ref`. It does not trigger scraping
or SMTP and leaves source pauses chosen by the owner intact.

`python -m app.collection_recovery_admin --through <closed-UTC-midnight>` previews
a missed-day recovery. Applying needs an approval reference, an explicit maximum
reservation and enough monthly allowance. It freezes eligible active identities
once, creates a durable 30-minute permit and delegates collection to the existing
coordinator. A repeated command returns the same cycle. It cannot replace jobs
whose outcome is uncertain, or collect paused/private/unapproved identities.

Provider specifications: [asynchronous limits](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests)
and the [Facebook input example](https://brightdata.com/products/web-scraper/social-media-scrape).
No guarantee is made for content that the provider omits, inaccessible/private
content, reposts without a verified event timestamp, saturated windows, or periods
deferred by the monthly ceiling. Those remain coverage gaps, never silent success.
