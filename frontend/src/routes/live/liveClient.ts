/**
 * Data aggregator for the Live TV dashboard. Pulls every existing endpoint
 * the page needs in parallel via Promise.allSettled — one section failing
 * (e.g. no wallet configured, a transient 500) degrades that section to
 * `null` rather than taking the whole page down, same discipline as
 * newsClient.ts's fetchNewsPipeline.
 *
 * "Today" is recomputed fresh on every call (see todayRange.ts) rather than
 * memoized by the caller, so a page left open across local midnight rolls
 * its counters over naturally on the next poll tick.
 */
import { fetchEquity, type EquityResponse } from '../activity/equityClient'
import { fetchNewsPipelineSince, type NewsPipelineTotals } from '../activity/newsClient'
import type { NewsPipelineCard } from '../activity/newsTypes'
import type { PositionRecord } from '../activity/portfolioTypes'
import { fetchWalletBalance, type WalletBalance } from '../activity/walletClient'
import { fetchHealthDetail, type HealthDetailResponse } from '../health/healthClient'
import { fetchStatistics, type StatisticsResponse } from '../statistics/statisticsClient'
import { todayRange } from './todayRange'

// Purely a display cap for the activity-feed ticker now — the "N today"
// stat cards read the backend's own `since`-scoped `total` (see
// newsClient.ts's fetchNewsPipelineSince), which is independent of this
// limit. Only the top ~20 of this ever render at once, so this just needs
// to comfortably cover that.
const NEWS_TICKER_LIMIT = 300

// GET /api/portfolio/equity's window_hours is an int that must be one of
// the backend's EQUITY_WINDOW_OPTIONS_HOURS safelist (1/6/12/24/168/720 —
// openpoly/api/portfolio_routes.py); anything else either 422s (a
// fractional value like "hours since midnight" isn't a valid int) or
// silently falls back to 24h. Round up to the smallest safelisted bucket
// that still covers today so far, same duplication convention
// OverviewTab.tsx's WINDOW_OPTIONS already uses for this safelist.
function equityWindowHours(hoursSinceMidnight: number): 1 | 6 | 12 | 24 {
  if (hoursSinceMidnight <= 1) return 1
  if (hoursSinceMidnight <= 6) return 6
  if (hoursSinceMidnight <= 12) return 12
  return 24
}

export type LiveSnapshot = {
  equity: EquityResponse | null
  wallet: WalletBalance | null
  statisticsToday: StatisticsResponse | null
  positions: PositionRecord[] | null
  /** Pipeline cards for the activity-feed ticker, capped at NEWS_TICKER_LIMIT. */
  newsPipelineToday: NewsPipelineCard[]
  /** Accurate, uncapped counts since local midnight — for the stat cards. */
  newsPipelineTotals: NewsPipelineTotals
  health: HealthDetailResponse | null
  fetchedAt: number
}

const EMPTY_TOTALS: NewsPipelineTotals = { news: 0, embedding: 0, analyzer: 0, entry: 0 }

// LIMIT_MAX on GET /api/positions (openpoly/api/portfolio_routes.py) — the
// route returns open+closed newest-first, unbounded by `limit` otherwise
// defaults to 100, so an open position older than the 100 most recent rows
// overall could silently drop off the Live page without this.
const POSITIONS_LIMIT = 500

async function fetchPositions(signal?: AbortSignal): Promise<PositionRecord[]> {
  const r = await fetch(`/api/positions?limit=${POSITIONS_LIMIT}`, { signal })
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  const body = (await r.json()) as { positions: PositionRecord[] }
  return body.positions
}

async function settle<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p
  } catch {
    return null
  }
}

export async function fetchLiveSnapshot(signal?: AbortSignal): Promise<LiveSnapshot> {
  const { since, hoursSinceMidnight } = todayRange()

  const [equity, wallet, statisticsToday, positions, newsPipeline, health] = await Promise.all([
    settle(fetchEquity(equityWindowHours(hoursSinceMidnight), signal)),
    settle(fetchWalletBalance(signal)),
    settle(fetchStatistics(since, null, signal)),
    settle(fetchPositions(signal)),
    settle(fetchNewsPipelineSince(since, NEWS_TICKER_LIMIT, signal)),
    settle(fetchHealthDetail(signal)),
  ])

  return {
    equity,
    wallet,
    statisticsToday,
    positions,
    newsPipelineToday: newsPipeline?.cards ?? [],
    newsPipelineTotals: newsPipeline?.totals ?? EMPTY_TOTALS,
    health,
    fetchedAt: Date.now() / 1000,
  }
}
