/** Data client for POST /api/backtest/run. */
import type { PositionRecord } from '../activity/portfolioTypes'
import type { PnlCurvePoint, StatisticsSummary } from '../statistics/statisticsClient'

export type { PnlCurvePoint } from '../statistics/statisticsClient'

export type SectionSelection = {
  module: string
  name: string
  config: Record<string, unknown>
}

export type BacktestRequestBody = {
  since: number
  until: number
  entry: SectionSelection
  exit: SectionSelection
}

export type BacktestResponse = {
  since: number | null
  until: number | null
  summary: StatisticsSummary
  pnl_curve: PnlCurvePoint[]
  // Backtest positions carry synthetic (negative) ids — same PositionRecord
  // shape as /api/statistics, but never a real database row.
  closed_positions: PositionRecord[]
  closed_positions_truncated: boolean
  replayed_analyzer_calls: number
  skipped_market_not_in_catalog: number
  elapsed_ms: number
}

export type RunBacktestResult =
  | { status: 'ok'; data: BacktestResponse }
  | { status: 'error'; error: string }

export async function runBacktest(body: BacktestRequestBody): Promise<RunBacktestResult> {
  let r: Response
  try {
    r = await fetch('/api/backtest/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
  } catch (e) {
    return { status: 'error', error: e instanceof Error ? e.message : String(e) }
  }
  if (!r.ok) {
    const errBody = (await r.json().catch(() => ({}))) as {
      detail?: { message?: string; error?: string } | string
    }
    const detail = errBody.detail
    const msg =
      typeof detail === 'string'
        ? detail
        : (detail?.message ?? detail?.error ?? `HTTP ${r.status}`)
    return { status: 'error', error: msg }
  }
  return { status: 'ok', data: (await r.json()) as BacktestResponse }
}
