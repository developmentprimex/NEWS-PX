# GA Time Table in the morning email

This optional integration attaches `GA-Time-Table-YYYY-MM-DD.pdf` and
`GA-Time-Table-YYYY-MM-DD.xlsx` to the recurring
07:00 News report. It uses the report cutoff date in Europe/Budapest, including DST,
and the existing Admin Tasks print layout with its single-day filter. The evening
report, per-source subscriptions and the configured News recipient are preserved.
An enabled morning attachment also sends on days without new News items.

Configure `NEWS_GA_TIMETABLE_BINDING_FILE` with the absolute path to a protected JSON
file outside the checkout and public web directories. Its fields are:

| Field | Value |
| --- | --- |
| `recipient` | The exact existing `NEWS_EMAIL_RECIPIENT`; a mismatch refuses export |
| `username` | `gane.arifaj` |
| `primeflow_python` | Absolute path to PrimeFlow's existing Python executable |
| `primeflow_backend` | Absolute path to the existing PrimeFlow backend with its private `.env` |
| `origin` | HTTPS origin of the PrimeFlow frontend |
| `api_origins` | Array of permitted HTTPS API origins used by that frontend |
| `browser_executable` | Absolute path to the installed Chrome/Chromium executable |
| `cache_directory` | Absolute protected directory for dated PDF snapshots |

No PrimeFlow credentials, tokens or database URLs belong in this binding. A small
subprocess runs under PrimeFlow's existing Python, with a read-only database
transaction, to mint the active GA user's access token. The token stays in memory
and the private subprocess pipe. The browser uses a fresh context, permits only
GET/HEAD/OPTIONS to the configured origins, blocks websockets and service workers,
and reads the existing GA rows, slots and Common View meeting data before printing.
No PrimeFlow application files or settings are changed.

PDF validation requires one page, a valid PDF signature, the exact dated heading
and the comment column. The Excel file uses the existing authenticated single-day
export as its layout and fills its four columns from the same frozen Print rows,
including comments, reminders and meetings. Values remain literal text, including
notes beginning with `=`. Its date, headers, single-day columns and all printed
cell values are verified before delivery. Row heights expand to fit the content.
Both formats reuse a private dated snapshot after success.
If export fails, no email claim or item-delivery receipt is created, the checkpoint
is preserved and Email settings shows that the morning report awaits PDF and Excel.
Once SMTP is claimed, the existing uncertain-outcome protection still applies:
never clear a claim or automatically resend after an ambiguous SMTP result.

For rollback, clear only `NEWS_GA_TIMETABLE_BINDING_FILE` and restart the reviewed
News API runtime. Keep dated snapshots private. Existing report claims and News
item receipts must be retained. Disabling the binding restores ordinary morning
reports that send only when new subscribed News items are available.
