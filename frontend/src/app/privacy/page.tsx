import type { Metadata } from "next"

export const metadata: Metadata = {
  title: "Privacy Policy | PrimEx News Intelligence",
  description: "How PrimEx News Intelligence handles workspace data, LinkedIn connections, approved reposts and privacy requests.",
  alternates: { canonical: "https://news.primexeu.com/privacy" },
}

const sections = [
  { id: "information", title: "Information we process" },
  { id: "linkedin", title: "LinkedIn connections and reposts" },
  { id: "providers", title: "Service providers and sharing" },
  { id: "cookies", title: "Cookies and security" },
  { id: "retention", title: "Retention and deletion" },
  { id: "choices", title: "Your choices and contact" },
]

export default function PrivacyPage() {
  const linkStyle = "rounded-sm text-[#205b3d] underline decoration-[#a8bdae] underline-offset-4 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-[#205b3d]"
  return <main className="min-h-screen bg-[#f7f9f6] px-5 py-8 text-[#263d30] sm:px-8 sm:py-12">
    <div className="mx-auto max-w-3xl">
      <header className="border-b border-[#dae4da] pb-7">
        <a href="https://primexeu.com" className="text-sm font-semibold tracking-wide">PrimEx <span className="font-normal text-[#617368]">/ News Intelligence</span></a>
        <h1 className="mt-8 text-4xl font-semibold tracking-tight sm:text-5xl">Privacy Policy</h1>
        <p className="mt-4 text-sm text-[#617368]">Effective date: <time dateTime="2026-10-22">22 October 2026</time></p>
        <p className="mt-5 text-base leading-7 text-[#4e6255]">This policy explains how PrimEx handles information in News Intelligence at <a className={linkStyle} href="https://news.primexeu.com">news.primexeu.com</a>, our internal workspace for reviewing public news and professional updates and, when enabled and authorized, approving LinkedIn reposts.</p>
        <p className="mt-3 text-sm leading-6 text-[#4e6255]">PrimEx operates this workspace. Contact us about this policy or your information at <a className={linkStyle} href="mailto:development@primexeu.com">development@primexeu.com</a>.</p>
      </header>

      <nav aria-label="Privacy policy contents" className="my-7 rounded-xl border border-[#e0e8de] bg-white p-5">
        <p className="text-xs font-semibold uppercase tracking-wider text-[#617368]">On this page</p>
        <ul className="mt-3 grid gap-x-6 gap-y-2 text-sm sm:grid-cols-2">{sections.map(section => <li key={section.id}><a className={linkStyle} href={`#${section.id}`}>{section.title}</a></li>)}</ul>
      </nav>

      <article className="space-y-9 text-[15px] leading-7 text-[#4e6255] [&_h2]:mb-3 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:tracking-tight [&_h2]:text-[#263d30] [&_h3]:mb-2 [&_h3]:font-semibold [&_h3]:text-[#263d30] [&_section]:scroll-mt-6">
        <section id="information" aria-labelledby="information-heading">
          <h2 id="information-heading">1. Information we process</h2>
          <p>We process information to provide the workspace, review relevant content, maintain security and carry out actions requested by authorized staff. This includes:</p>
          <ul className="mt-3 list-disc space-y-2 pl-5">
            <li><strong>Workspace access and preferences:</strong> the configured sign-in email and account identifier, session information, monitored sources, saved items, read status and email-report preferences.</li>
            <li><strong>Collected content:</strong> public source links, post or article text, publication dates, available author names and profile or company links, engagement counts and public comment previews returned by our collection services.</li>
            <li><strong>LinkedIn connections:</strong> the authorizing member identifier and display name, connected Page identifiers and names, granted permissions, authorization expiry and an encrypted access token.</li>
            <li><strong>Publishing records:</strong> the selected account and target post, optional commentary, who approved the action, approval and publication times, the result and the published post identifier when confirmed.</li>
            <li><strong>Operational information:</strong> request and error information needed to operate and troubleshoot the service. Our hosting and network providers may process IP addresses and related connection information.</li>
          </ul>
          <p className="mt-3">Information about other people may appear in public posts or comments collected for professional review. Public availability does not remove their ability to contact us about how their information is used.</p>
        </section>

        <section id="linkedin" aria-labelledby="linkedin-heading">
          <h2 id="linkedin-heading">2. LinkedIn connections and reposts</h2>
          <p>LinkedIn publishing is optional and depends on configuration, LinkedIn API access and the account owner or Page administrator authorizing the requested permissions. LinkedIn handles the authorization screen. We do not ask you to provide your LinkedIn password to News Intelligence.</p>
          <p className="mt-3">We use the connection to identify the account, verify Page publishing permissions where applicable, and submit a repost after explicit approval in the workspace. Connecting an account, reading a post or collecting updates does not approve publication. Reposts submitted by this integration are public on LinkedIn.</p>
          <p className="mt-3">Only the selected author account, target post identifier, optional approved commentary and information required for the publishing request are sent to LinkedIn. Publishing credentials are not included in AI-analysis prompts. The connection does not request access to private LinkedIn messages or your full personal feed.</p>
          <p className="mt-3">Public content collection is a separate function from LinkedIn account authorization. Disconnecting a publishing account stops the use of its saved credential; it does not automatically remove previously collected public content or posts already published on LinkedIn.</p>
        </section>

        <section id="providers" aria-labelledby="providers-heading">
          <h2 id="providers-heading">3. Service providers and sharing</h2>
          <p>Information is available to authorized workspace users and service providers as needed for the enabled features:</p>
          <ul className="mt-3 list-disc space-y-2 pl-5">
            <li><strong>LinkedIn:</strong> account authorization, Page permission checks and approved publication.</li>
            <li><strong>Bright Data:</strong> collection of configured public professional or social source content when that integration is enabled.</li>
            <li><strong>OpenAI:</strong> relevant collected text and review context for summaries, analysis or brief generation when AI features are enabled. Names or other information present in source text may be included.</li>
            <li><strong>Email providers:</strong> delivery of selected items and reports to configured recipients when email features are enabled.</li>
            <li><strong>Hosting, storage and network providers:</strong> operation, protection and availability of the service.</li>
          </ul>
          <p className="mt-3">Some providers may process information outside your country. Their handling of information is governed by their applicable terms, privacy notices and arrangements with us. We do not sell personal information or provide it to advertisers for targeted advertising. We may disclose information where legally required or necessary to investigate misuse and protect the service.</p>
          <p className="mt-3">LinkedIn independently controls information published on its platform. See <a className={linkStyle} href="https://www.linkedin.com/legal/privacy-policy">LinkedIn’s Privacy Policy</a> for its practices.</p>
        </section>

        <section id="cookies" aria-labelledby="cookies-heading">
          <h2 id="cookies-heading">4. Cookies and security</h2>
          <p>We use an essential session cookie to keep authorized users signed in and a short-lived cookie to bind a LinkedIn authorization attempt to the browser that started it. The application may use browser storage for saved or read preferences in its sample-data mode. It does not include advertising cookies or advertising trackers.</p>
          <p className="mt-3">We use access controls and encrypted storage for LinkedIn access tokens. These controls reduce risk but cannot guarantee that every transmission or storage system is completely secure. Never send passwords, access tokens or other credentials in a privacy-request email.</p>
        </section>

        <section id="retention" aria-labelledby="retention-heading">
          <h2 id="retention-heading">5. Retention and deletion</h2>
          <p>Workspace content and preferences are retained while needed for the workspace or until removed through an available control or a request to us. Connecting an account does not start a fixed automatic deletion period for all stored content.</p>
          <p className="mt-3">A LinkedIn access token is stored in encrypted form while the connection is retained. Expired authorizations cannot publish and require reconnection. Selecting <strong>Disconnect</strong> removes the locally saved encrypted credential. Connection identifiers and publishing receipts remain unless separately removed so that past actions can be reviewed and accidental duplicate reposts can be prevented.</p>
          <p className="mt-3">You can request deletion of connection information, collected information about you and associated records by contacting us. We review the request, confirm the requester’s authority where needed, and remove data subject to any applicable legal or security retention requirements. Revoking an app on LinkedIn does not itself erase our separate stored records; contact us for their deletion.</p>
          <p className="mt-3">Backups may retain copies until they are replaced or removed through the backup process. Deleting a local record does not delete a repost from LinkedIn. Published reposts can be managed on LinkedIn by the account owner or an authorized Page administrator.</p>
        </section>

        <section id="choices" aria-labelledby="choices-heading">
          <h2 id="choices-heading">6. Your choices and contact</h2>
          <p>You can choose whether to connect LinkedIn, approve each repost, and use available workspace preferences. To stop the saved publishing connection, open <strong>INT Linkedin Activity → Manage LinkedIn accounts → Disconnect</strong>. You can also revoke the app’s authorization through LinkedIn’s <strong>Settings &amp; Privacy → Data privacy → Permitted services</strong>.</p>
          <div className="mt-4 rounded-xl border border-[#d7e4d5] bg-white p-5">
            <h3>Request access, correction or deletion</h3>
            <p>Email <a className={linkStyle} href="mailto:development@primexeu.com?subject=News%20Intelligence%20privacy%20request">development@primexeu.com</a> with the subject <strong>News Intelligence privacy request</strong>. Describe the information or connected account concerned and the action you are requesting. For a public post, include its link. Do not include your password or access token.</p>
          </div>
          <p className="mt-3">Depending on the law applicable to you, you may have rights to access, correct or delete personal information, object to or restrict processing, withdraw consent, or raise a concern with a relevant data-protection authority. Withdrawing authorization stops future authorized actions; it does not undo a post already published.</p>
          <p className="mt-3">This workspace is intended for authorized professional users and is not directed at children. We may update this policy when the service or its handling of information changes. The date at the top identifies the current version.</p>
        </section>
      </article>

      <footer className="mt-10 flex flex-wrap items-center justify-between gap-3 border-t border-[#dae4da] pt-5 text-sm text-[#617368]">
        <span>PrimEx News Intelligence</span>
        <a className={linkStyle} href="/login">Workspace sign in</a>
      </footer>
    </div>
  </main>
}
