/** Data client for GET /api/statistics — the Statistics page's combined
 * summary + P&L curve + closed-trades response. */
import type { PositionRecord } from '../activity/portfolioTypes'

export type StatisticsSummary = {
  positions_opened: number
  positions_closed: number
  wins: number
  losses: number
  breakeven: number
  win_rate: number | null
  gross_profit: number
  gross_loss: number
  net_pnl: number
  profit_factor: number | null
  average_win: number | null
  average_loss: number | null
  largest_win: number | null
  largest_loss: number | null
  average_hold_seconds: number | null
  close_reason_breakdown: Record<string, number>
  // Percent-return companions — each trade's return as realized_pnl / (qty
  // * avg_entry_price), the same cost-basis formula the exit monitor's
  // take-profit/stop-loss thresholds use.
  net_pnl_pct: number | null
  average_win_pct: number | null
  average_loss_pct: number | null
  largest_win_pct: number | null
  largest_loss_pct: number | null
}

export type PnlCurvePoint = { ts: number; cumulative_pnl: number }

export type StatisticsResponse = {
  since: number | null
  until: number | null
  summary: StatisticsSummary
  // Reuses the existing PositionRecord shape rather than a bespoke DTO —
  // /api/statistics augments each row with market_question the same way
  // /api/positions does, so this is a real superset match.
  closed_positions: PositionRecord[]
  pnl_curve: PnlCurvePoint[]
  closed_positions_truncated: boolean
}

export async function fetchStatistics(
  since: number | null,
  until: number | null,
  signal?: AbortSignal,
): Promise<StatisticsResponse> {
  const params = new URLSearchParams()
  if (since !== null) params.set('since', String(since))
  if (until !== null) params.set('until', String(until))
  const qs = params.toString()
  const r = await fetch(`/api/statistics${qs ? `?${qs}` : ''}`, { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as StatisticsResponse
}
