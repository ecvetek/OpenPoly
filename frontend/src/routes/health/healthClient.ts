// Backend route: GET /api/health/detail (openpoly/api/health_routes.py).
// Distinct from the frozen liveness probe at GET /api/health.

export type SubsystemStatus = 'ok' | 'degraded' | 'down' | 'stopped' | 'disabled'

export type SubsystemCheck = {
  status: SubsystemStatus
  detail: Record<string, unknown>
}

export type HealthDetailResponse = {
  status: SubsystemStatus
  timestamp: number
  checks: Record<string, SubsystemCheck>
}

export async function fetchHealthDetail(signal?: AbortSignal): Promise<HealthDetailResponse> {
  const r = await fetch('/api/health/detail', { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as HealthDetailResponse
}
