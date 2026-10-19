# LinkedIn fetch and refresh review — 19 October 2026

The local collection path was reviewed and corrected. Provider calls in validation use HTTP mocks and a disposable local PostgreSQL database; this review does not establish live repost recall or deploy the changes.

## Collection flow

1. `main.py` schedules `check_due_sources` every five minutes when collection is enabled. This scan does not mean a billable request every five minutes: LinkedIn normally runs daily, with inactivity backoff and explicitly enabled high-priority extras.
2. `check_source` selects active sources. `protected_linkedin_check` holds a PostgreSQL advisory lock for the canonical LinkedIn account across commits, reloads the current source state, and checks the collection owner. Historical duplicate source rows remain intact.
3. `collect_linkedin` checks configuration, source pauses and provider-account state. Existing reserved snapshots are recovered before considering another trigger. A durable budget reservation is committed before the billable POST. Ambiguous trigger outcomes stay paused rather than issuing another POST.
4. Discovery sends company/profile URL, a date window, and a record cap. Personal discovery includes `only_authored_posts=false`. Routine windows overlap the previous successful date and are bounded to seven days; older history needs an explicit backfill.
5. Later scans poll and download the known snapshot. Raw returned-record usage persists before analysis. Parsing checks direct LinkedIn URLs and source attribution, normalizes timestamps, and retains repost/original-author details.
6. `_store_post` deduplicates within a source and refreshes observed activity. Existing item IDs and saved/read references remain stable. Text edits rerun analysis; engagement-only refreshes do not.
7. Only valid, unsaturated, dated, in-window routine results advance the successful-fetch checkpoint. Partial/saturated results retain the checkpoint and expose the coverage warning. Web feed refreshes read stored data and cannot start LinkedIn provider jobs.

## Corrections

| Finding | Resulting behavior |
| --- | --- |
| A monitored original author could incorrectly qualify another person's repost as their own activity. | Explicit reposter attribution takes precedence; unidentified or different reposters are rejected. |
| Profile attribution trusted an arbitrary host with an `/in/...` path. | Author URLs must resolve to a valid LinkedIn actor identity. Explicit company-author URLs are checked too. |
| Company reposters on localized LinkedIn hosts were rejected inconsistently. | Profile and company actors use the same host, actor-type and decoded-slug validation. |
| A malformed URL could abort parsing of the entire snapshot. | Invalid post URLs are skipped; malformed optional author/comment URLs become unknown. Valid rows can still be saved, while invalid rows prevent checkpoint advancement. |
| Offset publication dates were compared with UTC collection windows using their local calendar date. | Provider timestamps are normalized to UTC before window checks. |
| Sparse repeat observations erased stored text, counts, comments and repost metadata. | Missing fields retain previous observations; observed zero counts still update. Missing comment previews preserve earlier comments unless an explicit zero count clears them. |
| Duplicate rows inside one snapshot were skipped before refreshing activity. | Every usable row reaches storage deduplication, allowing later rows to contribute observed fields without another item. |
| A session could reuse stale source state after another worker completed a job or paused the source. | Source state is reloaded while holding the canonical advisory lock, before ownership checks and collection. |
| The provider-account row could remain stale in SQLAlchemy's identity map despite a new locking query. | The locked account query refreshes existing ORM state, honoring pauses committed by another session. |

## Provider contract and remaining bounds

The current [Bright Data asynchronous request specification](https://docs.brightdata.com/api-reference/rest-api/scraper/asynchronous-requests) explicitly states that `limit_per_input` caps `/trigger` results when passed in the query, and recommends an object body as another supported form. The existing bounded query request was retained. Earlier repository analysis described this compatibility as unverified; the current documentation now addresses it. Mock tests verify emitted requests, not remote enforcement.

The daily cadence, six-record default, saturation pause, billing gates and backfill controls remain deliberate coverage/spend limits. A successful provider job cannot establish exhaustive plain/quote repost coverage. In particular, a plain repost carrying only its original post's publication date may fall outside the collection window; it is retained for review without treating the window as complete. No repost time is invented from scrape time. Deduplication remains source-scoped; this change does not introduce global content/activity identity or distinguish repeated share actions when the provider supplies no separate activity identity.

## Validation

Regression tests cover attribution, localized identities, malformed records, UTC boundaries, sparse refreshes, duplicate-row enrichment, stale source/account sessions, snapshot recovery and source deactivation. Existing integration coverage also checks budget serialization, concurrent workers, trigger ambiguity, restart recovery, read/saved preservation, saturation, protected web refreshes and guarded canaries.

The full backend run passed 221 tests and exposed one unrelated calendar-dependent email fixture: its fixed 18 October 2026 deadline had expired, correctly changing priority to Normal. The test now pins the scoring reference date to the fixture's publication date; the affected email/focus tests were rerun. Production email/priority logic was not changed. The three frontend LinkedIn monitoring tests also passed.
