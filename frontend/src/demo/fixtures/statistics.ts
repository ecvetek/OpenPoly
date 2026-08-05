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
  // Index-aligned with winPnls/lossPnls (null where cost basis is 0) —
  // mirrors openpoly/portfolio/statistics.py's build_statistics so
  // largest_win_pct/largest_loss_pct pair with the same trade as the
  // dollar figure, not an independently-maximized percent.
  const winPcts: (number | null)[] = []
  const lossPcts: (number | null)[] = []
  const holdSeconds: number[] = []
  const closeReasonBreakdown: Record<string, number> = {}
  let totalCostBasis = 0

  for (const p of closed) {
    const pnl = p.realized_pnl ?? 0
    const cost = p.qty * p.avg_entry_price
    const pct = cost > 0 ? pnl / cost : null
    if (cost > 0) totalCostBasis += cost
    if (pnl > 0) {
      wins += 1
      grossProfit += pnl
      winPnls.push(pnl)
      winPcts.push(pct)
    } else if (pnl < 0) {
      losses += 1
      grossLoss += -pnl
      lossPnls.push(pnl)
      lossPcts.push(pct)
    } else {
      breakeven += 1
    }
    holdSeconds.push((p.closed_at as number) - p.opened_at)
    const reason = p.close_reason ?? 'unknown'
    closeReasonBreakdown[reason] = (closeReasonBreakdown[reason] ?? 0) + 1
  }

  const netPnl = round2(grossProfit - grossLoss)
  const winPctsValid = winPcts.filter((p): p is number => p !== null)
  const lossPctsValid = lossPcts.filter((p): p is number => p !== null)
  const largestWinIdx = winPnls.length ? winPnls.indexOf(Math.max(...winPnls)) : -1
  const largestLossIdx = lossPnls.length ? lossPnls.indexOf(Math.min(...lossPnls)) : -1

  return {
    positions_opened: positions.length,
    positions_closed: closed.length,
    wins,
    losses,
    breakeven,
    win_rate: wins + losses > 0 ? wins / (wins + losses) : null,
    gross_profit: round2(grossProfit),
    gross_loss: round2(grossLoss),
    net_pnl: netPnl,
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
    net_pnl_pct: totalCostBasis > 0 ? netPnl / totalCostBasis : null,
    average_win_pct: winPctsValid.length
      ? winPctsValid.reduce((a, b) => a + b, 0) / winPctsValid.length
      : null,
    average_loss_pct: lossPctsValid.length
      ? lossPctsValid.reduce((a, b) => a + b, 0) / lossPctsValid.length
      : null,
    largest_win_pct: largestWinIdx >= 0 ? winPcts[largestWinIdx] : null,
    largest_loss_pct: largestLossIdx >= 0 ? lossPcts[largestLossIdx] : null,
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
