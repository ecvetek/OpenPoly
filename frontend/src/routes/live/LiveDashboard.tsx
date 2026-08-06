/**
 * Live — the TV dashboard. Auto-refreshing on a user-selectable interval
 * (5s/10s/30s/60s, see LiveHeader), composed entirely from existing
 * endpoints via fetchLiveSnapshot(). Hero row is all-time (mirrors
 * Overview's stat cards); the Today row and the pipeline/feed panels are
 * scoped to the local calendar day.
 */
import { useState } from 'react'
import { StatCard } from '../../components/StatCard'
import { usePoll } from '../activity/usePoll'
import { EquityChart } from '../activity/EquityChart'
import { formatPnl, pnlClass } from '../activity/format'
import { formatPercent } from '../statistics/format'
import { LiveActivityFeed } from './LiveActivityFeed'
import { LiveHeader } from './LiveHeader'
import { LiveOpenPositions } from './LiveOpenPositions'
import { LivePipelineFlow } from './LivePipelineFlow'
import { fetchLiveSnapshot, type LiveSnapshot } from './liveClient'

const DEFAULT_REFRESH_MS = 5000

function fmtUsd(n: number | null | undefined): string {
  return n === null || n === undefined ? '—' : `$${n.toFixed(2)}`
}

export function LiveDashboard() {
  const [refreshMs, setRefreshMs] = useState(DEFAULT_REFRESH_MS)
  const { data, status, error } = usePoll<LiveSnapshot>(fetchLiveSnapshot, refreshMs)

  if (data === null) {
    return (
      <div className="grid place-items-center flex-1">
        <p className="text-sm text-neutral-400">
          {status === 'error' ? `Backend unreachable: ${error}` : 'Loading…'}
        </p>
      </div>
    )
  }

  const {
    equity,
    wallet,
    statisticsToday,
    positions,
    newsPipelineToday,
    newsPipelineTotals,
    health,
  } = data
  const s = statisticsToday?.summary ?? null
  const eq = equity?.summary ?? null
  const noWallet = wallet !== null && !wallet.configured
  const aiCallsToday = newsPipelineTotals.embedding + newsPipelineTotals.analyzer

  return (
    <div className="flex-1 min-h-0 overflow-y-auto flex flex-col gap-4 px-4 sm:px-6 pb-6">
      <LiveHeader
        health={health}
        fetchedAt={data.fetchedAt}
        refreshMs={refreshMs}
        onRefreshChange={setRefreshMs}
      />

      {status === 'error' && (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
          Backend unreachable; showing last known data.
        </div>
      )}

      {/* Hero row — all-time, mirrors Overview's stat cards. */}
      <div className="grid grid-cols-2 xl:grid-cols-4 gap-3">
        <StatCard
          label="Wallet total"
          size="lg"
          value={noWallet ? 'No wallet' : fmtUsd(wallet?.total ?? null)}
          tone={noWallet ? 'text-neutral-500' : 'text-neutral-100'}
          sub={
            noWallet || wallet?.usdc == null
              ? undefined
              : `cash ${fmtUsd(wallet.usdc)} + positions ${fmtUsd(wallet.positions_value)}`
          }
        />
        <StatCard
          label="Total P&L"
          size="lg"
          value={eq === null ? '—' : formatPnl(eq.total)}
          tone={pnlClass(eq?.total ?? null)}
          sub={eq === null ? undefined : `realized ${formatPnl(eq.realized)} · unrealized ${formatPnl(eq.unrealized)}`}
        />
        <StatCard
          label="Open positions"
          size="lg"
          value={eq === null ? '—' : String(eq.open_positions)}
          tone="text-neutral-100"
        />
        <StatCard
          label="System"
          size="lg"
          value={health === null ? '—' : health.status.toUpperCase()}
          tone={
            health === null
              ? 'text-neutral-500'
              : health.status === 'ok'
                ? 'text-emerald-300'
                : health.status === 'degraded'
                  ? 'text-amber-300'
                  : 'text-red-300'
          }
        />
      </div>

      {/* Today row. */}
      <div className="grid grid-cols-2 sm:grid-cols-3 xl:grid-cols-6 gap-3">
        <StatCard label="News today" value={String(newsPipelineTotals.news)} tone="text-neutral-100" />
        <StatCard
          label="AI calls today"
          value={String(aiCallsToday)}
          tone="text-neutral-100"
          sub={`embed ${newsPipelineTotals.embedding} · analyze ${newsPipelineTotals.analyzer}`}
        />
        <StatCard label="Opened today" value={s === null ? '—' : String(s.positions_opened)} tone="text-neutral-100" />
        <StatCard label="Closed today" value={s === null ? '—' : String(s.positions_closed)} tone="text-neutral-100" />
        <StatCard
          label="Won / lost today"
          value={s === null ? '—' : `${s.wins} – ${s.losses}`}
          tone={s === null ? 'text-neutral-100' : s.wins >= s.losses ? 'text-emerald-300' : 'text-red-300'}
        />
        <StatCard label="Win rate today" value={s === null ? '—' : formatPercent(s.win_rate)} tone="text-neutral-100" />
      </div>

      {/* Main content. */}
      <div className="grid grid-cols-1 xl:grid-cols-3 gap-4 flex-1 min-h-[420px]">
        <div className="xl:col-span-2 flex flex-col gap-4 min-h-0">
          <div className="rounded border border-neutral-800 p-3">
            <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">
              Equity curve · today · realized + unrealized (mark @ bid)
            </div>
            {equity === null || equity.points.length === 0 ? (
              <div className="h-64 grid place-items-center text-[11px] text-neutral-500">
                No trades yet today.
              </div>
            ) : (
              <EquityChart points={equity.points} />
            )}
          </div>
          <LivePipelineFlow
            newsCount={newsPipelineTotals.news}
            embeddingCount={newsPipelineTotals.embedding}
            analyzerCount={newsPipelineTotals.analyzer}
            openedToday={s?.positions_opened ?? 0}
            closedToday={s?.positions_closed ?? 0}
          />
        </div>

        <div className="flex flex-col gap-4 min-h-0">
          <div className="flex-1 min-h-[220px]">
            <LiveOpenPositions positions={positions ?? []} />
          </div>
          <div className="flex-1 min-h-[220px]">
            <LiveActivityFeed cards={newsPipelineToday} />
          </div>
        </div>
      </div>
    </div>
  )
}
