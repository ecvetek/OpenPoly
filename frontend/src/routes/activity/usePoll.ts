/**
 * Mount-scoped polling hook for the Activity tabs.
 *
 * The initial fetch runs unconditionally; only the interval is gated on tab
 * visibility, so a background tab does not keep hitting the API but also never
 * hangs forever on "Loading…". The fetcher is held in a ref so passing an
 * inline arrow does not re-subscribe the effect.
 *
 * `intervalMs = null` disables automatic refreshing entirely (no interval
 * timer, no visibilitychange-triggered refetch) — the fetcher still runs
 * once on mount so the initial data loads, but nothing after that short of
 * a full remount or a changed `refreshKey`.
 *
 * `refreshKey` is an escape hatch for callers whose fetcher closes over
 * state that should force an immediate refetch when it changes (e.g. a
 * selected time window), independent of `intervalMs` — including while
 * refresh is disabled. Passing nothing (`undefined`) is a no-op: it never
 * changes, so it never affects the existing interval-only behavior.
 *
 * The returned `refetch()` is the same mechanism exposed as a manual
 * trigger: it bumps an internal nonce that's folded into the effect's
 * dependency array, forcing an immediate `refresh()` on demand — e.g. for a
 * manual refresh button — without waiting for `intervalMs` or a
 * `refreshKey` change.
 */
import { useEffect, useLayoutEffect, useRef, useState } from 'react'

export type PollStatus = 'loading' | 'ready' | 'error'

export type PollResult<T> = {
  data: T | null
  status: PollStatus
  error: string | null
  refetch: () => void
}

export function usePoll<T>(
  fetcher: () => Promise<T>,
  intervalMs: number | null = 3000,
  refreshKey?: unknown,
): PollResult<T> {
  const [data, setData] = useState<T | null>(null)
  const [status, setStatus] = useState<PollStatus>('loading')
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const fetcherRef = useRef(fetcher)
  useLayoutEffect(() => {
    fetcherRef.current = fetcher
  })

  useEffect(() => {
    let cancelled = false
    // Guards against overlapping fetches: if a slow response is still
    // in flight when the next interval tick or visibilitychange fires,
    // skip re-issuing rather than risk an older response landing after
    // (and overwriting) a newer one.
    let inflight = false
    async function refresh() {
      if (inflight) return
      inflight = true
      try {
        const result = await fetcherRef.current()
        if (cancelled) return
        setData(result)
        setStatus('ready')
        setError(null)
      } catch (e) {
        if (cancelled) return
        setStatus('error')
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        inflight = false
      }
    }
    void refresh()
    if (intervalMs === null) {
      return () => {
        cancelled = true
      }
    }
    const maybeRefresh = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    const timer = setInterval(maybeRefresh, intervalMs)
    document.addEventListener('visibilitychange', maybeRefresh)
    return () => {
      cancelled = true
      clearInterval(timer)
      document.removeEventListener('visibilitychange', maybeRefresh)
    }
  }, [intervalMs, refreshKey, nonce])

  const refetch = () => setNonce((n) => n + 1)
  return { data, status, error, refetch }
}
