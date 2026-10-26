# Source quality

The source's ACTIVE/PAUSED preference is separate from its technical collection
state. The Sources page distinguishes setup, collection holds, access failures,
empty results and incomplete coverage. Website/RSS sources also expose their
actual collection-enabled flag. A successful read with no qualifying current
articles is shown as "No current announcements".

Generic website collection rejects navigation, privacy/terms, service and test
pages, and requires publication markup before accepting an HTML article. Scoped
chapters and embassy sources reject unrelated global content. Advertised feeds
are filtered before their result limit; an unrelated global feed does not prevent
local article discovery. Tracking parameters are removed from discovered URLs,
and storage recognizes existing website/RSS URLs with equivalent tracking links.
Generic imports are limited to the preceding 90 days and reject future dates
more than one hour ahead. Dedicated official collectors retain their own rules.

The reversible `news-quality-excluded:<item UUID without hyphens>` metadata keys
remove reviewed historical imports from ordinary feeds, briefs and scheduled
email. Original items, analyses, read/saved state and delivery receipts remain
stored. Saved-only queries retain access to bookmarked material. Initial cleanup
also quarantines stale initial website imports and duplicate website URLs.

Unknown publication dates are labelled as collection times on cards. Public
Facebook profile records still require exact source attribution. Attributed
events can be retained, but event and shared-original timestamps do not certify
an announcement/share time and cannot make that activity eligible for scheduled
email. Attribution and coverage holds remain enforced for unverifiable records.

Private groups require a public alternative. HTTP access denials and certificate
failures require an accessible official source or action by the website owner.
Empty provider results do not certify that no activity occurred. Collection
continues under the existing schedule and shared budget.
