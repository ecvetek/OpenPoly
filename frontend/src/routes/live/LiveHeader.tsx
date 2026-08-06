/**
 * Live page header — overall system status, paper/live mode, a self-ticking
 * wall clock (independent of the data poll, so the page never looks frozen
 * even between polls), and how stale the last fetch is.
 */
import { useEffect, useState } from 'react'
import { Dot } from '../../components/Dot'
import { ago } from '../../lib/time'
import type { HealthDetailResponse, SubsystemStatus } from '../health/healthClient'

const STATUS_COLOR: Record<SubsystemStatus, string> = {
  ok: '#34d399',
  degraded: '#f59e0b',
  down: '#ef4444',
  stopped: '#6b7280',
  disabled: '#6b7280',
}

const STATUS_COPY: Record<SubsystemStatus, string> = {
  ok: 'All systems healthy',
  degraded: 'Degraded',
  down: 'Down',
  stopped: 'Stopped',
  disabled: 'Disabled',
}

function useClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function LiveHeader({
  health,
  fetchedAt,
}: {
  health: HealthDetailResponse | null
  fetchedAt: number | null
}) {
  const clock = useClock()
  const execMode = health?.checks.market_access?.detail.exec_mode as string | undefined
  const isLive = execMode === 'live'

  return (
    <div className="flex items-center gap-4 flex-wrap px-4 sm:px-6 pt-4 pb-3">
      <h1 className="text-2xl font-semibold text-neutral-100 tracking-tight">
        openPoly <span className="text-neutral-500 font-normal">· Live</span>
      </h1>

      <span
        className={`px-2.5 py-1 text-xs font-semibold uppercase tracking-wide rounded border ${
          isLive
            ? 'bg-red-900/40 text-red-300 border-red-700/60'
            : 'bg-neutral-800 text-neutral-300 border-neutral-700/60'
        }`}
        title={isLive ? 'LIVE — real funds' : 'Paper — no real funds'}
      >
        {execMode ?? '—'}
      </span>

      {health && (
        <div className="flex items-center gap-2 text-sm">
          <Dot color={STATUS_COLOR[health.status]} pulse={health.status !== 'ok'} />
          <span className="text-neutral-300">{STATUS_COPY[health.status]}</span>
        </div>
      )}

      <div className="ml-auto flex items-center gap-4">
        <span className="text-[11px] text-neutral-500">
          data as of {fetchedAt === null ? '—' : `${ago(fetchedAt)} ago`}
        </span>
        <span className="text-xl font-mono text-neutral-200 tabular-nums">{clock}</span>
      </div>
    </div>
  )
}
