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
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'

export type PollStatus = 'loading' | 'ready' | 'error'

export type PollResult<T> = {
  data: T | null
  status: PollStatus
  error: string | null
  refetch: () => void
}

export function usePoll<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
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
    // (and overwriting) a newer one. Also tracks the in-flight request's
    // controller so cleanup (unmount, or a dependency change starting a
    // fresh effect run) can actually abort it — `cancelled` alone only
    // stopped the stale response from updating state, the underlying
    // request kept running to completion regardless.
    let inflight = false
    let controller: AbortController | null = null
    async function refresh() {
      if (inflight) return
      inflight = true
      controller = new AbortController()
      try {
        const result = await fetcherRef.current(controller.signal)
        if (cancelled) return
        setData(result)
        setStatus('ready')
        setError(null)
      } catch (e) {
        // An aborted fetch throws here too, but `cancelled` is already true
        // by the time abort() is ever called (see cleanup below), so this
        // guard covers it — no separate AbortError check needed.
        if (cancelled) return
        setStatus('error')
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        inflight = false
        controller = null
      }
    }
    void refresh()
    if (intervalMs === null) {
      return () => {
        cancelled = true
        controller?.abort()
      }
    }
    const maybeRefresh = () => {
      if (document.visibilityState === 'visible') void refresh()
    }
    const timer = setInterval(maybeRefresh, intervalMs)
    document.addEventListener('visibilitychange', maybeRefresh)
    return () => {
      cancelled = true
      controller?.abort()
      clearInterval(timer)
      document.removeEventListener('visibilitychange', maybeRefresh)
    }
  }, [intervalMs, refreshKey, nonce])

  // Stable identity (setState setters never change) — lets callers that wrap
  // refetch in their own useCallback/useEffect deps (e.g. PositionDetail's
  // forceRefresh) get a dependency array eslint-exhaustive-deps can actually
  // satisfy, instead of a new function every render.
  const refetch = useCallback(() => setNonce((n) => n + 1), [])
  return { data, status, error, refetch }
}
