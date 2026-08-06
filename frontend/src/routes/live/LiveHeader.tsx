/**
 * Live page header — overall system status, paper/live mode, a self-ticking
 * 24-hour wall clock (independent of the data poll, so the page never looks
 * frozen even between polls), how stale the last fetch is, and the refresh-
 * interval selector.
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

// This page runs unattended, so unlike Overview/Statistics/Health's picker
// there's no "off" option.
const REFRESH_OPTIONS: ReadonlyArray<{ readonly label: string; readonly ms: number }> = [
  { label: '5s', ms: 5000 },
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
  { label: '60s', ms: 60000 },
]

function useClock(): string {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return now.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  })
}

export function LiveHeader({
  health,
  fetchedAt,
  refreshMs,
  onRefreshChange,
}: {
  health: HealthDetailResponse | null
  fetchedAt: number | null
  refreshMs: number
  onRefreshChange: (ms: number) => void
}) {
  const clock = useClock()
  const execMode = health?.checks.market_access?.detail.exec_mode as string | undefined
  const isLive = execMode === 'live'

  return (
    <div className="flex items-center gap-4 flex-wrap px-4 sm:px-6 pt-4 pb-3">
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
        <div className="flex items-center gap-1 text-[10px] text-neutral-500">
          <span>refresh</span>
          {REFRESH_OPTIONS.map((opt) => (
            <button
              key={opt.label}
              type="button"
              onClick={() => onRefreshChange(opt.ms)}
              className={`rounded px-1.5 py-0.5 ${
                refreshMs === opt.ms
                  ? 'bg-blue-600 text-white'
                  : 'border border-neutral-700 text-neutral-400'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <span className="text-[11px] text-neutral-500">
          data as of {fetchedAt === null ? '—' : `${ago(fetchedAt)} ago`}
        </span>
        <span className="text-xl font-mono text-neutral-200 tabular-nums">{clock}</span>
      </div>
    </div>
  )
}
