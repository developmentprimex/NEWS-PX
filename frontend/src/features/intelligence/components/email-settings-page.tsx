"use client"

import * as React from "react"
import Link from "next/link"
import { ArrowLeft, Mail, X } from "lucide-react"
import { toast } from "sonner"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { useAuth } from "@/lib/auth"
import type { DigestSettings } from "../types"

const dateFormat = new Intl.DateTimeFormat("sq-AL", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })

export function EmailSettingsPage() {
  const { apiFetch } = useAuth()
  const [settings, setSettings] = React.useState<DigestSettings | null>(null)
  const [times, setTimes] = React.useState<string[]>([])
  const [changed, setChanged] = React.useState(false)
  const [loading, setLoading] = React.useState(true)
  const [error, setError] = React.useState("")
  const [saving, setSaving] = React.useState(false)
  const [testing, setTesting] = React.useState(false)

  React.useEffect(() => {
    let active = true
    void apiFetch("/intelligence/digest-settings").then(async (response) => {
      if (!response.ok) throw new Error("Cilësimet e emailit nuk u ngarkuan.")
      const payload = await response.json() as DigestSettings
      if (active) { setSettings(payload); setTimes(payload.times) }
    }).catch((cause) => { if (active) setError(cause instanceof Error ? cause.message : "Cilësimet nuk u ngarkuan.") })
      .finally(() => { if (active) setLoading(false) })
    return () => { active = false }
  }, [apiFetch])

  const save = async () => {
    setSaving(true)
    try {
      const response = await apiFetch("/intelligence/digest-settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ times }),
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "Orari nuk u ruajt.")
      setSettings(payload as DigestSettings)
      setTimes((payload as DigestSettings).times)
      setChanged(false)
      toast.success("Orari i raportit u ruajt.")
    } catch (cause) { toast.error(cause instanceof Error ? cause.message : "Orari nuk u ruajt.") }
    finally { setSaving(false) }
  }

  const testEmail = async () => {
    setTesting(true)
    try {
      const response = await apiFetch("/intelligence/digest-settings/test", { method: "POST" })
      if (!response.ok) throw new Error("Emaili test nuk u dërgua. Kontrollo konfigurimin SMTP.")
      toast.success("Emaili test u dërgua.")
    } catch (cause) { toast.error(cause instanceof Error ? cause.message : "Emaili test nuk u dërgua.") }
    finally { setTesting(false) }
  }

  return <div className="min-h-screen px-4 pb-16 pt-7 text-[#24342a] sm:px-7 lg:px-10 lg:pt-10"><div className="mx-auto max-w-[900px]">
    <Link href="/intelligence" className="inline-flex items-center gap-1.5 text-xs font-medium text-[#7b8b80] hover:text-[#314e3a]"><ArrowLeft className="size-3.5" /> Intelligence</Link>
    <p className="mt-6 text-xs font-semibold uppercase tracking-[0.14em] text-[#668b71]">Njoftimet</p>
    <h1 className="mt-2 text-3xl font-semibold tracking-tight">Email settings</h1>
    <p className="mt-2 text-sm leading-6 text-[#69796e]">Raporte të përbashkëta me lajmet, postimet, repostimet dhe artikujt e rinj nga burimet ku ke aktivizuar emailin.</p>
    {loading ? <p role="status" className="mt-8 text-sm text-[#718076]">Duke ngarkuar cilësimet…</p> : error ? <p role="alert" className="mt-8 rounded-xl border border-red-200 bg-red-50 p-4 text-sm text-red-800">{error}</p> : settings ? <section className="mt-8 rounded-2xl border border-[#dbe6da] bg-white p-5 sm:p-6">
      <div className="flex items-center gap-2 text-[#356447]"><Mail className="size-5" /><h2 className="text-lg font-semibold">Raporti me email</h2></div>
      <p className="mt-2 text-sm text-[#69796e]">Raporti përfshin aktivitetet e intervalit nga burimet e zgjedhura, të grupuara sipas burimit dhe llojit. Çdo aktivitet dërgohet një herë. Zona kohore: {settings.timezone}.</p>
      <p className="mt-2 text-sm text-[#69796e]">Raporti dërgohet vetëm kur ka përditësime të reja. Burimet pa përditësime nuk shfaqen dhe aktivitetet e dërguara nuk përsëriten.</p>
      {settings.scheduleManaged ? <div className="mt-5 rounded-lg bg-[#edf3ed] p-4 text-sm text-[#356447]">
        <p className="font-semibold">07:00 dhe 21:00 · Europe/Budapest</p>
        <p className="mt-2 leading-6">{settings.socialDailyFetch ? "LinkedIn dhe Facebook kontrollohen një herë në ditë, në 00:05 UTC, për ditën e plotë UTC që sapo përfundoi. Email-et mbeten në 07:00 dhe 21:00; website dhe RSS kontrollohen edhe në këto dy orare. Përditësimet me datë të paverifikuar nuk paraqiten si publikime të intervalit." : "LinkedIn dhe Facebook kontrollohen dy herë në ditë. Emaili dërgohet pasi përfundon mbledhja, bashkë me përditësimet nga News, website dhe RSS. Burimet e tjera vazhdojnë kontrollet periodike."}</p>
      </div> : <div className="mt-5 flex flex-wrap items-end gap-3">
        {times.map((value, index) => <div key={index} className="flex items-end gap-1"><div><Label htmlFor={`digest-time-${index}`} className="mb-1.5 block text-xs">Orari {index + 1}</Label><Input id={`digest-time-${index}`} type="time" value={value} onChange={(event) => { setChanged(true); setTimes((current) => current.map((time, i) => i === index ? event.target.value : time)) }} className="w-32" /></div>{times.length > 1 ? <Button type="button" variant="ghost" size="icon-sm" aria-label={`Hiq orarin ${index + 1}`} title="Hiq orarin" onClick={() => { setChanged(true); setTimes((current) => current.filter((_, i) => i !== index)) }}><X className="size-4" /></Button> : null}</div>)}
        {times.length < 8 ? <Button type="button" variant="outline" size="sm" onClick={() => { setChanged(true); setTimes((current) => [...current, "09:00"]) }}>Shto orar</Button> : null}
        <Button type="button" size="sm" disabled={!changed || saving || times.some((value) => !value) || new Set(times).size !== times.length} onClick={() => void save()} className="bg-[#275037] text-white hover:bg-[#1e3d2a]">{saving ? "Duke ruajtur…" : "Ruaj oraret"}</Button>
      </div>}
      <div className="mt-5 space-y-1 text-xs text-[#718076]"><p>Marrësi: {settings.recipient}</p>{settings.lastSentAt ? <p>Raporti i fundit: {dateFormat.format(new Date(settings.lastSentAt))}</p> : null}{!settings.emailConfigured ? <p role="status" className="text-[#9a6e40]">SMTP nuk është konfiguruar ende. Dërgimi nis pasi të vendosen kredencialet e emailit në server.</p> : null}{settings.lastError ? <p role="alert" className="text-red-700">Dërgimi i fundit dështoi: {settings.lastError}</p> : null}</div>
      <Button type="button" variant="outline" size="sm" className="mt-4" disabled={!settings.emailConfigured || testing} onClick={() => void testEmail()}>{testing ? "Duke dërguar…" : "Dërgo email test"}</Button>
    </section> : null}
    <p className="mt-6 text-sm text-[#69796e]">Për të zgjedhur çfarë hyn në raport, aktivizo emailin te <Link href="/intelligence/sources" className="font-medium text-[#356447] underline">Sources</Link>.</p>
  </div></div>
}
