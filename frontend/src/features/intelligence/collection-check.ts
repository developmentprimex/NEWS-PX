export type ApiFetch = (path: string, init?: RequestInit) => Promise<Response>
export type SourceCheckStatus = {
  state: "idle" | "running" | "finished"
  total: number; processed: number; failed: number; started: number; pending: number
  partial?: number; blocked?: number
  cycle_id?: string | null; phase?: string | null; cutoff_at?: string | null
}

async function readStatus(response: Response): Promise<SourceCheckStatus> {
  const payload = await response.json().catch(() => null)
  if (!response.ok) throw new Error(typeof payload?.detail === "string" ? payload.detail : "Could not check sources.")
  if (!payload || !["idle", "running", "finished"].includes(payload.state)) throw new Error("Could not read check progress.")
  return payload as SourceCheckStatus
}

export async function startCollectionCheck(apiFetch: ApiFetch): Promise<SourceCheckStatus> {
  return readStatus(await apiFetch("/intelligence/sources/check-all", { method: "POST" }))
}

export async function loadCollectionStatus(apiFetch: ApiFetch): Promise<SourceCheckStatus> {
  return readStatus(await apiFetch("/intelligence/sources/check-all/status"))
}
