# LinkedIn publishing setup

The website supports explicit, public native reposts through LinkedIn's Posts API. This is independent of Bright Data and its collection budget. Nothing is published by a collector, scheduler, connection callback or page refresh.

## What the account owner needs to do

1. Create/select an application at <https://www.linkedin.com/developers/apps>. Complete LinkedIn's required app details and Page association/verification when requested.
2. Under **Products**, request **Share on LinkedIn** (`w_member_social`) and **Sign In with LinkedIn using OpenID Connect** (`openid`, `profile`). The personal connection requests these three scopes and does not request email or reading access to your personal feed.
3. Under **Auth**, register the exact redirect URL displayed in the website's **Repost from this website** panel. It is `NEWS_PUBLIC_ORIGIN` followed by `/api/intelligence/linkedin/callback`.
   - Local default: `http://localhost:3101/api/intelligence/linkedin/callback`.
   - Public deployment: `https://YOUR-NEWS-DOMAIN/api/intelligence/linkedin/callback`.
   - LinkedIn documents HTTPS callbacks for server applications. Use the public HTTPS origin if the portal does not accept a local development URL. The browser must already be signed into News at that same origin when connecting.
4. Put the **Client ID** and **Client Secret** into the server's private environment file, as shown below. Never commit these values, send the secret in chat, or expose it in frontend variables.
5. After the new version and configuration are running, open **INT Linkedin Activity**, choose **Manage LinkedIn accounts → Connect personal LinkedIn**, and authorize using the intended LinkedIn account. Multiple personal accounts can be connected individually. LinkedIn controls which signed-in account it authorizes; switch accounts on LinkedIn before connecting another one.
6. On a collected LinkedIn post, choose **Review & repost**, select the publishing account, review the exact target link, optionally add text, then select **Approve & repost**. This publishes immediately to a public feed. A successful result includes a link to the new repost.

## Private server configuration

```dotenv
NEWS_PUBLIC_ORIGIN=https://YOUR-NEWS-DOMAIN
NEWS_COOKIE_SECURE=true
NEWS_LINKEDIN_CLIENT_ID=YOUR-CLIENT-ID
NEWS_LINKEDIN_CLIENT_SECRET=YOUR-CLIENT-SECRET
NEWS_LINKEDIN_TOKEN_KEY=YOUR-FERNET-KEY
NEWS_LINKEDIN_API_VERSION=202607
NEWS_LINKEDIN_ORGANIZATION_ENABLED=false
```

Generate an encryption key once in a trusted terminal with the backend dependencies installed:

```powershell
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that output into `NEWS_LINKEDIN_TOKEN_KEY`. Keep it with the private deployment secrets and preserve it across restarts/deployments. Saved access tokens are encrypted with this key; changing or losing it requires accounts to reconnect. Local HTTP development uses `NEWS_COOKIE_SECURE=false` and the matching local public origin.

The existing deployment process must ship the new backend and frontend, then restart the backend with these settings. Application startup creates three new tables (`news_linkedin_connections`, `news_linkedin_oauth_states`, `news_linkedin_reposts`) without modifying collected posts. Existing production release approvals still apply. A source-code change alone does not update a running installation.

## Company Pages

Request the **Community Management API** product/access for the app. LinkedIn must grant `w_organization_social` and `r_organization_admin`; product availability/approval is controlled by LinkedIn. Enable `NEWS_LINKEDIN_ORGANIZATION_ENABLED=true` only after these permissions are available.

In **Manage LinkedIn accounts**, enter the Page's numeric ID (available in its Page admin URL) and choose **Connect company Page**. Authenticate as an administrator/content administrator. The backend checks approved organization roles through LinkedIn and retrieves the Page's name before saving a company connection. Connecting a personal account does not automatically authorize a company Page.

The company flow requests `openid profile w_organization_social r_organization_admin`. It follows the role finder pagination for up to 1,000 role entries; an unverified Page connection is refused. LinkedIn also enforces your current role and permissions on publication.

## Post identity and coverage

The API requires the original `urn:li:share:…` or `urn:li:ugcPost:…` identifier. An `activity-…` link contains a different kind of identifier; changing its prefix is unsafe. The collector preserves exact native post identifiers when returned by its provider. Older posts can be resolved from their explicit native link or a bounded, read-only request to the public post's canonical metadata. Login pages, off-site redirects and suggested/embedded post links cannot identify the target.

When LinkedIn does not expose a usable identifier, the dialog asks for an exact native post link/URN. Check the displayed exact target link before approval. Without that identifier, repost manually using the source link. This integration does not claim that every collected activity can be reposted automatically. No new Bright Data jobs are triggered to resolve an identifier. Post visibility, deletion and disabled reposting can also cause LinkedIn to refuse publication.

## Receipts, duplicate prevention and connection lifecycle

Each approval records the News user, stable author URN, native parent URN, item, commentary, approval time and result. A database uniqueness constraint protects the same author/parent across duplicate collected sources and reconnects. The durable publishing claim is committed before the API POST; concurrent clicks/tabs cannot publish twice. Different explicitly selected author accounts can repost the same parent separately.

Only an explicit **Approve & repost** sends a publishing request. The optional commentary is escaped for LinkedIn's `little` text format and is treated as literal text, without converting typed text into special mentions/annotations.

Statuses:

- `published`: LinkedIn returned HTTP 201 and a valid post URN. The receipt links to that repost.
- `failed`: LinkedIn definitively rejected the request. Correct access/identity issues and explicitly approve another attempt. HTTP 401 marks the credential expired until reconnecting.
- `publishing`: an approval was durably recorded and a request may be running, or the process ended before saving a response. Refresh the status and check the author's LinkedIn feed.
- `unknown`: a network timeout, server error, conflict or incomplete success response made publication uncertain. Check the author's LinkedIn feed. The application blocks further requests for that author/parent.

The application never automatically retries a publish. `publishing` and `unknown` cannot be cleared through the user interface. An operator must reconcile the exact author/parent against LinkedIn before any database correction; reconnecting or disconnecting cannot clear the receipt. Mark a verified successful receipt `published` with its exact post URN, or only after independently establishing that no publication occurred mark it `failed` so a new explicit approval is possible. Do not reset an uncertain receipt just because time has passed. This favors avoiding duplicates after crashes/timeouts, at the cost of manual reconciliation for uncertain results.

Disconnect deletes the locally saved encrypted credential, while retaining the author identity and repost receipts. It does not delete posts or revoke LinkedIn's app grant; the account owner can revoke that in LinkedIn's permitted services settings. Expiring access tokens require a fresh connection; this implementation does not assume refresh-token access.

## Verification

Backend publishing tests use an isolated PostgreSQL database and mocked official LinkedIn HTTP. They cover OAuth binding/replay, credential encryption, roles/scopes, connection ownership, CSRF, publication approval, duplicate concurrent clicks, failures and uncertain outcomes. No test sends a live repost. LinkedIn app approval and the first owner-approved real repost remain necessary to verify account-specific access.

## Official references

- [OAuth authorization code flow](https://learn.microsoft.com/en-us/linkedin/shared/authentication/authorization-code-flow)
- [Share on LinkedIn permissions](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/share-on-linkedin)
- [OpenID Connect and member information](https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2)
- [Posts API and native reshares](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-07)
- [Organization access control](https://learn.microsoft.com/en-us/linkedin/marketing/community-management/organizations/organization-access-control-by-role?view=li-lms-2026-07)
- [Activity URNs can identify different objects](https://learn.microsoft.com/en-us/linkedin/compliance/integrations/compliance-events/decoration-change-with-activityurn)
