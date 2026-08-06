/**
 * BacktestResultDashboard — renders a completed backtest run using the same
 * StatCard/WinLossBar/PnlCurveChart/CloseReasonBreakdown/ClosedPositionsTable
 * components the live Statistics page uses, so a backtest result reads
 * exactly like the real thing. ClosedPositionsTable renders with
 * linkable={false} — backtest positions carry synthetic (negative) ids that
 * don't exist in the real database, so a /positions/:id link would be
 * broken/misleading.
 */
import { StatCard } from '../../components/StatCard'
import { ClosedPositionsTable } from '../statistics/ClosedPositionsTable'
import { CloseReasonBreakdown } from '../statistics/CloseReasonBreakdown'
import {
  formatDuration,
  formatPercent,
  formatPnl,
  formatPnlPercent,
  pnlClass,
} from '../statistics/format'
import { PnlCurveChart } from '../statistics/PnlCurveChart'
import { WinLossBar } from '../statistics/WinLossBar'
import type { BacktestResponse, PnlCurvePoint } from './backtestClient'

// Duplicated from StatisticsDashboard.tsx (module-private there) — same
// peak-to-trough walk over the ascending cumulative-P&L curve, cheap enough
// to duplicate for one extra caller rather than exporting a helper across
// route folders for a five-line function.
function maxDrawdown(points: readonly PnlCurvePoint[]): number {
  let peak = 0
  let worst = 0
  for (const p of points) {
    if (p.cumulative_pnl > peak) peak = p.cumulative_pnl
    const drawdown = peak - p.cumulative_pnl
    if (drawdown > worst) worst = drawdown
  }
  return worst
}

export function BacktestResultDashboard({ data }: { data: BacktestResponse }) {
  const s = data.summary
  const drawdown = maxDrawdown(data.pnl_curve)

  return (
    <div className="flex flex-col gap-4">
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label="Analyzer calls replayed"
          value={String(data.replayed_analyzer_calls)}
          tone="text-neutral-100"
        />
        <StatCard
          label="Skipped · market not in catalog"
          value={String(data.skipped_market_not_in_catalog)}
          tone={data.skipped_market_not_in_catalog > 0 ? 'text-amber-300' : 'text-neutral-100'}
          sub="market_id no longer in the live catalog"
        />
        <StatCard label="Positions opened" value={String(s.positions_opened)} tone="text-neutral-100" />
        <StatCard label="Elapsed" value={`${data.elapsed_ms}ms`} tone="text-neutral-100" />
      </div>

      <WinLossBar wins={s.wins} losses={s.losses} breakeven={s.breakeven} />

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard
          label="Win rate"
          value={formatPercent(s.win_rate)}
          tone="text-neutral-100"
          sub="excl. breakeven"
        />
        <StatCard
          label="Net P&L"
          value={formatPnl(s.net_pnl)}
          tone={pnlClass(s.net_pnl)}
          pctValue={s.net_pnl_pct === null ? undefined : formatPnlPercent(s.net_pnl_pct)}
        />
        <StatCard
          label="Profit factor"
          value={s.profit_factor === null ? '—' : s.profit_factor.toFixed(2)}
          tone="text-neutral-100"
        />
        <StatCard label="Max drawdown" value={formatPnl(-drawdown)} tone={pnlClass(-drawdown)} />
        <StatCard label="Avg hold time" value={formatDuration(s.average_hold_seconds)} tone="text-neutral-100" />
        <StatCard
          label="Avg win"
          value={s.average_win === null ? '—' : formatPnl(s.average_win)}
          tone={pnlClass(s.average_win)}
          pctValue={s.average_win_pct === null ? undefined : formatPnlPercent(s.average_win_pct)}
        />
        <StatCard
          label="Avg loss"
          value={s.average_loss === null ? '—' : formatPnl(s.average_loss)}
          tone={pnlClass(s.average_loss)}
          pctValue={s.average_loss_pct === null ? undefined : formatPnlPercent(s.average_loss_pct)}
        />
      </div>

      <CloseReasonBreakdown breakdown={s.close_reason_breakdown} />

      <div className="rounded border border-neutral-800 p-3">
        <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">
          Cumulative realized P&L · backtest
        </div>
        {data.pnl_curve.length === 0 ? (
          <div className="h-64 grid place-items-center text-[11px] text-neutral-500">
            No closed positions in this backtest.
          </div>
        ) : (
          <PnlCurveChart points={data.pnl_curve} />
        )}
      </div>

      <ClosedPositionsTable
        positions={data.closed_positions}
        truncated={data.closed_positions_truncated}
        linkable={false}
      />
    </div>
  )
}
