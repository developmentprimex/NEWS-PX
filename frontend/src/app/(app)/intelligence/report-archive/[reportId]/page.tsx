"use client"

import Link from "next/link"
import { useParams } from "next/navigation"
import { useEffect, useState } from "react"

import { useAuth } from "@/lib/auth"

type ArchiveItem = { title: string; url: string; source: string }

export default function ReportArchivePage() {
  const { reportId } = useParams<{ reportId: string }>()
  const { apiFetch, user } = useAuth()
  const requestKey = user ? `${user.id}:${reportId}` : null
  const [result, setResult] = useState<{ key: string; items: ArchiveItem[] | null } | null>(null)
  const archive = result?.key === requestKey ? result : null

  useEffect(() => {
    if (!requestKey) return
    let current = true
    void apiFetch(`/intelligence/digest-archive/${encodeURIComponent(reportId)}`)
      .then(async (response) => {
        if (!response.ok) throw new Error("Archive unavailable")
        const archive: ArchiveItem[] = await response.json()
        if (current) setResult({ key: requestKey, items: archive })
      })
      .catch(() => { if (current) setResult({ key: requestKey, items: null }) })
    return () => { current = false }
  }, [apiFetch, reportId, requestKey])

  return (
    <main className="mx-auto max-w-4xl space-y-5 px-6 py-10">
      <Link href="/intelligence/news" className="text-sm text-[#35624a] underline">Kthehu te lajmet</Link>
      <h1 className="text-3xl font-semibold">Arkivi i raportit</h1>
      {!archive ? <p role="status">Duke ngarkuar arkivin…</p> : !archive.items ? <p role="alert">Arkivi nuk është i disponueshëm.</p> :
        archive.items.length === 0 ? <p>Raporti nuk ka artikuj në arkiv.</p> :
          <ul className="divide-y divide-[#e1e8e0] rounded-xl border border-[#e1e8e0] bg-white px-5">
            {archive.items.map((item, index) => (
              <li key={`${item.source}-${item.url}-${index}`} className="py-4">
                <p className="mb-1 text-sm text-[#596d61]">{item.source}</p>
                {/^(https?):\/\//i.test(item.url)
                  ? <a href={item.url} className="font-medium underline" target="_blank" rel="noreferrer">{item.title}</a>
                  : <span>{item.title}</span>}
              </li>
            ))}
          </ul>}
    </main>
  )
}
