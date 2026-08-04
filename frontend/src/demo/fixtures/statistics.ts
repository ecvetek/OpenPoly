/**
 * Demo fixtures — Statistics page.
 *
 * Serves GET /api/statistics. Ignores since/until (same convention as the
 * Overview equity mock — a static, deterministic fixture rather than a
 * range-aware one — see ./activity's buildEquity) and derives its numbers
 * from the same closed positions already defined in ./activity, so the
 * Statistics page and the Activity → Positions tab agree when someone flips
 * between them.
 *
 * Required, not optional: an unregistered /api/* route falls back to a
 * silent 200 {} in mockServer.ts, and this app has no React error boundary
 * — StatisticsDashboard reading `data.summary.win_rate` off `{}` would
 * throw and white-screen the whole demo, not just this tab.
 */
import type { MockRoute } from '../mockServer'
import type {
  PnlCurvePoint,
  StatisticsResponse,
  StatisticsSummary,
} from '../../routes/statistics/statisticsClient'
import { positions } from './activity'

const closed = positions
  .filter((p) => p.status === 'closed' && p.closed_at !== null)
  .slice()
  .sort((a, b) => (a.closed_at as number) - (b.closed_at as number)) // ascending — matches the real endpoint's curve order

function round2(n: number): number {
  return Math.round(n * 100) / 100
}

function buildSummary(): StatisticsSummary {
  let wins = 0
  let losses = 0
  let breakeven = 0
  let grossProfit = 0
  let grossLoss = 0
  const winPnls: number[] = []
  const lossPnls: number[] = []
  const holdSeconds: number[] = []
  const closeReasonBreakdown: Record<string, number> = {}

  for (const p of closed) {
    const pnl = p.realized_pnl ?? 0
    if (pnl > 0) {
      wins += 1
      grossProfit += pnl
      winPnls.push(pnl)
    } else if (pnl < 0) {
      losses += 1
      grossLoss += -pnl
      lossPnls.push(pnl)
    } else {
      breakeven += 1
    }
    holdSeconds.push((p.closed_at as number) - p.opened_at)
    const reason = p.close_reason ?? 'unknown'
    closeReasonBreakdown[reason] = (closeReasonBreakdown[reason] ?? 0) + 1
  }

  return {
    positions_opened: positions.length,
    positions_closed: closed.length,
    wins,
    losses,
    breakeven,
    win_rate: wins + losses > 0 ? wins / (wins + losses) : null,
    gross_profit: round2(grossProfit),
    gross_loss: round2(grossLoss),
    net_pnl: round2(grossProfit - grossLoss),
    profit_factor: grossLoss > 0 ? round2(grossProfit / grossLoss) : null,
    average_win: winPnls.length ? round2(grossProfit / winPnls.length) : null,
    average_loss: lossPnls.length
      ? round2(lossPnls.reduce((a, b) => a + b, 0) / lossPnls.length)
      : null,
    largest_win: winPnls.length ? Math.max(...winPnls) : null,
    largest_loss: lossPnls.length ? Math.min(...lossPnls) : null,
    average_hold_seconds: holdSeconds.length
      ? holdSeconds.reduce((a, b) => a + b, 0) / holdSeconds.length
      : null,
    close_reason_breakdown: closeReasonBreakdown,
  }
}

function buildPnlCurve(): PnlCurvePoint[] {
  let cumulative = 0
  return closed.map((p) => {
    cumulative += p.realized_pnl ?? 0
    return { ts: p.closed_at as number, cumulative_pnl: round2(cumulative) }
  })
}

const statisticsResponse: StatisticsResponse = {
  since: null,
  until: null,
  summary: buildSummary(),
  pnl_curve: buildPnlCurve(),
  closed_positions: closed.slice().reverse(), // newest-first, matching the real endpoint
  closed_positions_truncated: false,
}

export const statisticsRoutes: MockRoute[] = [
  {
    pattern: /^\/api\/statistics$/,
    handler: () => statisticsResponse,
  },
]
