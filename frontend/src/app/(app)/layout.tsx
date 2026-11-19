"use client"

import * as React from "react"
import Link from "next/link"
import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { Bookmark, BriefcaseBusiness, ChevronRight, Compass, LayoutDashboard, Linkedin, LogOut, Mail, Menu, Newspaper, Radio, Sparkles, Users, X, Zap } from "lucide-react"
import { useAuth } from "@/lib/auth"
import { cn } from "@/lib/utils"
import { filters } from "@/features/intelligence/components/news-filters"

const sections = [
  { label: "Overview", href: "/intelligence", icon: LayoutDashboard },
  { label: "For You", href: "/intelligence/for-you", icon: Sparkles },
  { label: "INT Linkedin Activity", href: "/intelligence/primex-linkedin", icon: Linkedin },
  { label: "News", href: "/intelligence/news", icon: Newspaper },
  { label: "Opportunities", href: "/intelligence/opportunities", icon: BriefcaseBusiness },
  { label: "Saved", href: "/intelligence/saved", icon: Bookmark },
  { label: "Email settings", href: "/intelligence/email-settings", icon: Mail },
  { label: "Sources", href: "/intelligence/sources", icon: Radio },
  { label: "Users", href: "/intelligence/users", icon: Users, adminOnly: true },
]

export default function WorkspaceLayout({ children }: { children: React.ReactNode }) {
  return <React.Suspense fallback={<div className="min-h-screen bg-[#f7f8f5]" />}><WorkspaceShell>{children}</WorkspaceShell></React.Suspense>
}

function WorkspaceShell({ children }: { children: React.ReactNode }) {
  const router = useRouter()
  const pathname = usePathname()
  const searchParams = useSearchParams()
  const { user, loading, logout, apiFetch } = useAuth()
  const [menuOpen, setMenuOpen] = React.useState(false)
  const [repostUnread, setRepostUnread] = React.useState(0)

  React.useEffect(() => {
    if (!user) return
    let active = true
    const refresh = async () => {
      try {
        const response = await apiFetch("/intelligence/repost-watch/status")
        if (!response.ok) return
        const status = await response.json() as { unread: number }
        if (active) setRepostUnread(status.unread)
      } catch {}
    }
    void refresh()
    const timer = window.setInterval(() => void refresh(), 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [user, apiFetch, pathname])

  React.useEffect(() => { if (!loading && !user) router.replace("/login") }, [loading, user, router])
  React.useEffect(() => { setMenuOpen(false) }, [pathname, searchParams])
  React.useEffect(() => {
    if (!menuOpen) return
    const closeOnEscape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenuOpen(false) }
    window.addEventListener("keydown", closeOnEscape)
    return () => window.removeEventListener("keydown", closeOnEscape)
  }, [menuOpen])

  if (loading || !user) return <div className="flex min-h-screen items-center justify-center bg-[#f7f8f5] text-sm text-[#647369]">Loading workspace…</div>

  const highPriorityActive = pathname === "/intelligence/news" && searchParams.get("priority") === "high"
  const activeCategory = highPriorityActive ? "" : searchParams.get("category") || (pathname === "/intelligence/for-you" ? "For You" : pathname === "/intelligence" || pathname === "/intelligence/news" ? "All" : "")

  return <div className="min-h-screen bg-[#f7f8f5] text-[#24342a]">
    <header className="sticky top-0 z-30 flex h-16 items-center justify-between border-b border-[#e4e9e1] bg-white px-4 lg:hidden">
      <Link href="/intelligence" className="flex items-center gap-2.5 text-sm font-bold tracking-tight"><span className="flex size-8 items-center justify-center rounded-lg bg-[#254634] text-white"><Compass className="size-4" /></span> News Intelligence</Link>
      <button type="button" aria-label={menuOpen ? "Close menu" : "Open menu"} aria-expanded={menuOpen} aria-controls="workspace-sidebar" onClick={() => setMenuOpen((open) => !open)} className="rounded-lg p-2 text-[#415647] hover:bg-[#f0f4ef] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#54775d]">{menuOpen ? <X className="size-5" /> : <Menu className="size-5" />}</button>
    </header>
    {menuOpen ? <button type="button" aria-label="Close menu" onClick={() => setMenuOpen(false)} className="fixed inset-0 top-16 z-20 bg-[#172a1d]/35 lg:hidden" /> : null}
    <aside id="workspace-sidebar" className={cn("fixed bottom-0 left-0 top-16 z-30 flex w-[264px] flex-col border-r border-[#e5eae2] bg-white transition-transform lg:top-0 lg:translate-x-0 lg:visible", menuOpen ? "visible translate-x-0" : "invisible -translate-x-full")}>
      <Link href="/intelligence" className="hidden h-20 items-center gap-3 border-b border-[#eef1ec] px-6 text-[15px] font-bold tracking-tight lg:flex"><span className="flex size-9 items-center justify-center rounded-xl bg-[#254634] text-white"><Compass className="size-5" /></span><span>News Intelligence</span></Link>
      <div className="flex-1 overflow-y-auto px-3 py-5">
        <p className="px-3 text-[10px] font-bold uppercase tracking-[0.16em] text-[#94a096]">Workspace</p>
        <nav aria-label="Workspace sections" className="mt-3 space-y-1">
          {sections.filter((section) => !section.adminOnly || user.role === "ADMIN").map(({ label, href, icon: Icon }) => <Link key={href} href={href} aria-current={pathname === href ? "page" : undefined} className={cn("group flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#54775d]", pathname === href ? "bg-[#eaf1e9] text-[#214a31]" : "text-[#64746a] hover:bg-[#f5f7f3] hover:text-[#253c2c]")}><Icon className="size-[18px]" /><span className="flex-1">{label}</span>{href === "/intelligence/primex-linkedin" && repostUnread > 0 ? <span className="rounded-full bg-[#dcebdd] px-2 py-0.5 text-[10px] font-semibold" aria-label={`${repostUnread} unread SMM list posts`}>{repostUnread}</span> : null}{pathname === href ? <ChevronRight className="size-3.5 text-[#668672]" /> : null}</Link>)}
        </nav>
        <div className="my-6 border-t border-[#eef1ec]" />
        <p className="px-3 text-[10px] font-bold uppercase tracking-[0.16em] text-[#94a096]">Quick view</p>
        <Link href="/intelligence/news?priority=high" aria-current={highPriorityActive ? "page" : undefined} className={cn("mt-3 flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#54775d]", highPriorityActive ? "bg-[#eaf1e9] text-[#214a31]" : "text-[#64746a] hover:bg-[#f5f7f3] hover:text-[#253c2c]")}><Zap className="size-[18px]" /> High priority</Link>
        <div className="my-6 border-t border-[#eef1ec]" />
        <p className="px-3 text-[10px] font-bold uppercase tracking-[0.16em] text-[#94a096]">Explore topics</p>
        <nav aria-label="News topics" className="mt-3 space-y-0.5">
          {filters.map((filter) => {
            const href = filter === "For You" ? "/intelligence/for-you" : filter === "All" ? "/intelligence/news" : `/intelligence/news?category=${encodeURIComponent(filter)}`
            const active = pathname !== "/intelligence/sources" && activeCategory === filter
            return <Link key={filter} href={href} aria-current={active ? "page" : undefined} className={cn("flex items-center gap-3 rounded-lg px-3 py-2 text-[13px] transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[#54775d]", active ? "bg-[#f2f6ef] font-semibold text-[#25543a]" : "text-[#718077] hover:bg-[#f7f9f5] hover:text-[#284c35]")}><span className={cn("size-1.5 rounded-full", active ? "bg-[#477b53]" : "bg-[#c7d0c6]")} />{filter}</Link>
          })}
        </nav>
      </div>
      <div className="border-t border-[#eef1ec] p-4"><p className="truncate px-2 pb-2 text-xs text-[#83958a]" title={user.email}>{user.email}</p><button type="button" onClick={() => void logout().then(() => router.replace("/login"))} className="flex w-full items-center gap-3 rounded-lg px-2 py-2 text-left text-sm text-[#68776d] hover:bg-[#f5f7f3] hover:text-[#294633]"><LogOut className="size-4" /> Sign out</button></div>
    </aside>
    <main className="min-w-0 lg:pl-[264px]">{children}</main>
  </div>
}
