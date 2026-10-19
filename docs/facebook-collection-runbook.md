# Facebook collection pilot

Owner-approved scope: resolve the saved Facebook share links, add public page/group
collection to the existing protected pipeline, then test one page and one public
group before expanding. Personal profiles and private content are not approved.

The Facebook adapter uses async Scraper API datasets `gd_lkaxegm826bjpoo9m5`
(page posts) and `gd_lz11l67o2cb3r0lkj3` (group posts). The public machine-readable
schemas at `https://docs.brightdata.com/scrapers/{dataset_id}.json` verify the URL,
`num_of_posts`, `start_date` and `end_date` inputs. Dates use MM-DD-YYYY. Both
`num_of_posts` and `limit_per_input` bound output. Snapshot GETs reuse the existing
transport. Ambiguous POSTs retain reservations and cannot automatically retry.

`NEWS_FACEBOOK_FETCH_ENABLED` defaults false. Only canonical URLs explicitly in
`NEWS_FACEBOOK_APPROVED_SOURCE_URLS` may collect. Pilot defaults: three records per
routine request, twelve reserved/day, three hundred/month. These are additional
Facebook sublimits; all requests also consume the shared 130/day, 4000/month
operational budget, preserving 1000 reserve within the verified 5000 allowance.
Same-account Facebook requires an explicit private dataset-to-meter mapping and
fresh proof summing all three approved resource meters exactly once. Unknown
nonzero usage, changed account/key/plan/pricing, stale or inconsistent proof block
new Facebook requests. Update the hourly billing observer to the same validator
before activating Facebook, so it does not reject approved Facebook usage.

Source verification is a bounded public GET-only redirect/canonical-page check.
It never sends provider credentials to Facebook. Unsupported redirects, login
pages and ambiguous identities remain setup-needed. The Sources menu offers
Verify Facebook link and displays distinct Facebook/LinkedIn/Website icons plus
setup guidance. Source identity edits/deletes preserve existing paid history.

During an active one-day manual/evening trial, approved Facebook sources join the
frozen inventory. Exact `(successful checkpoint, cutoff]` activity timestamps
govern storage for the trial and email selection. Date-only/naive/shared-original
times are unverified; they stay feed-only. Failure, unknown timestamps and cap
saturation do not advance confirmed cycle coverage. Outside the trial the normal
closed-UTC-day scheduler applies; this feature does not extend trial duration.

Keep all Email on selections and the configured recipient unchanged. No test
email is part of the pilot. The report can include Facebook only if its existing
source subscription is enabled. Adding the connector does not subscribe sources.

After verified build/release, protected backup and stopped-API preservation check,
resolve sources and activate only the two proven public identities. Run the private
operator loading reviewed code/root configuration as `app.facebook_admin` with
`--source-url` and `--approval-ref`. Each identity receives a durable one-use claim
and a one-record reservation. Repeating the command only polls that snapshot;
it never reuses a consumed approval for a new POST. Saturation remains partial.
Record returned rows, reservations and settled provider usage separately. A
delayed meter is provisional and must not be described as a final zero cost.
