/**
 * StatisticsDashboard — realized trading-performance report for a selected
 * date range: activity counts, win/loss breakdown, P&L figures, a
 * cumulative-P&L chart, and the underlying closed trades. Fetches on mount
 * and whenever the range changes — no auto-refresh, unlike Health/Overview;
 * this is a look-back/review page, not a live monitor.
 */
import { useMemo, useState } from 'react'
import { RefreshButton } from '../../components/RefreshButton'
import { StatCard } from '../../components/StatCard'
import { usePoll } from '../activity/usePoll'
import { ClosedPositionsTable } from './ClosedPositionsTable'
import { CloseReasonBreakdown } from './CloseReasonBreakdown'
import { DateRangeControl } from './DateRangeControl'
import { DEFAULT_RANGE, resolveRange, type DateRange } from './dateRange'
import { formatDuration, formatPercent, formatPnl, pnlClass } from './format'
import { PnlCurveChart } from './PnlCurveChart'
import { fetchStatistics, type StatisticsResponse } from './statisticsClient'
import { WinLossBar } from './WinLossBar'

export function StatisticsDashboard() {
  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE)
  // Memoized on `range`, not recomputed inline in the render body —
  // resolveRange calls Date.now() internally for preset mode, so without
  // this, since/until would drift by a few ms on every render (not just
  // range changes), thrashing usePoll's refreshKey and the displayed range.
  const { since, until } = useMemo(() => resolveRange(range), [range])

  const { data, status, error, refetch } = usePoll<StatisticsResponse>(
    () => fetchStatistics(since, until),
    null, // no auto-refresh — fetch on mount + on range change only
    range,
  )

  if (data === null) {
    return (
      <div className="grid place-items-center p-10">
        <p className="text-sm text-neutral-400">
          {status === 'error' ? `Backend unreachable: ${error}` : 'Loading…'}
        </p>
      </div>
    )
  }

  const s = data.summary

  return (
    <div className="px-4 sm:px-6 pb-6 flex flex-col gap-4">
      {status === 'error' && (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
          Backend unreachable; data may be stale.
        </div>
      )}

      <div className="flex items-center justify-between flex-wrap gap-2">
        <DateRangeControl value={range} onChange={setRange} />
        <RefreshButton onClick={refetch} title="Refresh" />
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
        <StatCard
          label="Positions opened"
          value={String(s.positions_opened)}
          tone="text-neutral-100"
          sub="opened in range"
        />
        <StatCard
          label="Positions closed"
          value={String(s.positions_closed)}
          tone="text-neutral-100"
          sub="closed in range · feeds stats below"
        />
        <StatCard label="Avg hold time" value={formatDuration(s.average_hold_seconds)} tone="text-neutral-100" />
      </div>

      <WinLossBar wins={s.wins} losses={s.losses} breakeven={s.breakeven} />

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Win rate" value={formatPercent(s.win_rate)} tone="text-neutral-100" sub="excl. breakeven" />
        <StatCard label="Net P&L" value={formatPnl(s.net_pnl)} tone={pnlClass(s.net_pnl)} />
        <StatCard
          label="Profit factor"
          value={s.profit_factor === null ? '—' : s.profit_factor.toFixed(2)}
          tone="text-neutral-100"
        />
        <StatCard label="Gross profit" value={formatPnl(s.gross_profit)} tone={pnlClass(s.gross_profit)} />
        {/* gross_loss is stored as a positive magnitude (profit_factor needs
            both terms positive) — negate only here, at display time, so it
            reads red like the money-lost figure it represents. */}
        <StatCard label="Gross loss" value={formatPnl(-s.gross_loss)} tone={pnlClass(-s.gross_loss)} />
        <StatCard
          label="Avg win"
          value={s.average_win === null ? '—' : formatPnl(s.average_win)}
          tone={pnlClass(s.average_win)}
        />
        <StatCard
          label="Avg loss"
          value={s.average_loss === null ? '—' : formatPnl(s.average_loss)}
          tone={pnlClass(s.average_loss)}
        />
        <StatCard
          label="Largest win"
          value={s.largest_win === null ? '—' : formatPnl(s.largest_win)}
          tone={pnlClass(s.largest_win)}
        />
        <StatCard
          label="Largest loss"
          value={s.largest_loss === null ? '—' : formatPnl(s.largest_loss)}
          tone={pnlClass(s.largest_loss)}
        />
      </div>

      <CloseReasonBreakdown breakdown={s.close_reason_breakdown} />

      <div className="rounded border border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">
          Cumulative realized P&L · closed trades in range
        </div>
        {data.pnl_curve.length === 0 ? (
          <div className="h-64 grid place-items-center text-[11px] text-neutral-500">
            No closed positions in this range.
          </div>
        ) : (
          <PnlCurveChart points={data.pnl_curve} />
        )}
      </div>

      <ClosedPositionsTable positions={data.closed_positions} truncated={data.closed_positions_truncated} />
    </div>
  )
}
