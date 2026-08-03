/**
 * Health — at-a-glance status across every runtime subsystem (app, database,
 * market feed, trading/news feed, market access, pipeline, exit/settlement
 * monitors, embedding, reconciliation). Backed by GET /api/health/detail.
 *
 * Unlike Overview's default-off equity chart, this defaults to a live 5s
 * poll — it's meant to be glanced at while running, not a report you pull up
 * once and read.
 */
import { useState } from 'react'
import { Dot } from '../../components/Dot'
import { RefreshButton } from '../../components/RefreshButton'
import { ago } from '../../lib/time'
import { usePoll } from '../activity/usePoll'
import { HealthCard } from './HealthCard'
import { fetchHealthDetail, type HealthDetailResponse, type SubsystemStatus } from './healthClient'

const REFRESH_OPTIONS: ReadonlyArray<{ readonly label: string; readonly ms: number | null }> = [
  { label: 'off', ms: null },
  { label: '3s', ms: 3000 },
  { label: '5s', ms: 5000 },
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
]

// Fixed, explicit display order — the backend's `checks` object key order is
// not a display contract.
const CARD_ORDER: ReadonlyArray<{ readonly key: string; readonly label: string }> = [
  { key: 'app', label: 'App / API' },
  { key: 'database', label: 'Database' },
  { key: 'market_feed', label: 'Market Feed' },
  { key: 'news_feed', label: 'Trading / News Feed' },
  { key: 'market_access', label: 'Market Access' },
  { key: 'pipeline', label: 'Pipeline' },
  { key: 'exit_monitor', label: 'Exit Monitor' },
  { key: 'settlement_monitor', label: 'Settlement Monitor' },
  { key: 'embedding', label: 'Embedding' },
  { key: 'reconciliation', label: 'Reconciliation' },
]

const OVERALL_COPY: Record<SubsystemStatus, string> = {
  ok: 'All systems healthy',
  degraded: 'Degraded',
  down: 'Down',
  stopped: 'Stopped',
  disabled: 'Disabled',
}

const OVERALL_COLOR: Record<SubsystemStatus, string> = {
  ok: '#34d399',
  degraded: '#f59e0b',
  down: '#ef4444',
  stopped: '#6b7280',
  disabled: '#6b7280',
}

const OVERALL_BANNER_CLASS: Record<SubsystemStatus, string> = {
  ok: 'border-emerald-800/60 bg-emerald-950/30',
  degraded: 'border-amber-800/60 bg-amber-950/40',
  down: 'border-red-800/60 bg-red-950/40',
  stopped: 'border-neutral-800 bg-neutral-900/40',
  disabled: 'border-neutral-800 bg-neutral-900/40',
}

const OVERALL_TEXT_CLASS: Record<SubsystemStatus, string> = {
  ok: 'text-emerald-300',
  degraded: 'text-amber-300',
  down: 'text-red-300',
  stopped: 'text-neutral-300',
  disabled: 'text-neutral-300',
}

export function HealthDashboard() {
  const [refreshMs, setRefreshMs] = useState<number | null>(REFRESH_OPTIONS[2].ms)
  const { data, status, error, refetch } = usePoll<HealthDetailResponse>(fetchHealthDetail, refreshMs)

  return (
    <div className="px-6 pb-6 flex flex-col gap-4">
      <div className="flex items-center justify-end gap-3 flex-wrap pt-2">
        <div className="flex items-center gap-1 text-[10px] text-neutral-500">
          <span>refresh</span>
          {REFRESH_OPTIONS.map((opt) => (
            <button
              key={opt.label}
              type="button"
              onClick={() => setRefreshMs(opt.ms)}
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
        <RefreshButton onClick={refetch} title="Refresh health" />
      </div>

      {status === 'error' && (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
          Backend unreachable: {error}
        </div>
      )}

      {data === null ? (
        <div className="grid place-items-center p-10">
          <p className="text-sm text-neutral-400">
            {status === 'error' ? 'Waiting for backend…' : 'Loading…'}
          </p>
        </div>
      ) : (
        <>
          <div
            className={`rounded border px-4 py-3 flex items-center gap-3 ${OVERALL_BANNER_CLASS[data.status]}`}
          >
            <Dot color={OVERALL_COLOR[data.status]} pulse={data.status !== 'ok'} />
            <span className={`text-sm font-medium ${OVERALL_TEXT_CLASS[data.status]}`}>
              {OVERALL_COPY[data.status]}
            </span>
            <span className="ml-auto text-[11px] text-neutral-500">as of {ago(data.timestamp)} ago</span>
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
            {CARD_ORDER.map((c) => (
              <HealthCard key={c.key} label={c.label} check={data.checks[c.key]} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}
