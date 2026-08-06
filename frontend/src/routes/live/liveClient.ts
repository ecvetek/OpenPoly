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
import { fetchNewsPipeline } from '../activity/newsClient'
import type { NewsPipelineCard } from '../activity/newsTypes'
import type { PositionRecord } from '../activity/portfolioTypes'
import { fetchWalletBalance, type WalletBalance } from '../activity/walletClient'
import { fetchHealthDetail, type HealthDetailResponse } from '../health/healthClient'
import { fetchStatistics, type StatisticsResponse } from '../statistics/statisticsClient'
import { todayRange } from './todayRange'

// Same ceiling the backend enforces (SECTION_LOG_LIMIT_MAX / NEWS_LIMIT_MAX)
// — generous for a grain-scale bot's daily volume, but if a day's activity
// hits this exactly, `newsPipelineTruncated` flags that today's counts may
// undercount (mirrors `closed_positions_truncated` in /api/statistics).
const NEWS_PIPELINE_LIMIT = 500

export type LiveSnapshot = {
  equity: EquityResponse | null
  wallet: WalletBalance | null
  statisticsToday: StatisticsResponse | null
  positions: PositionRecord[] | null
  /** Every fetched pipeline card whose news arrived since local midnight. */
  newsPipelineToday: NewsPipelineCard[]
  newsPipelineTruncated: boolean
  health: HealthDetailResponse | null
  fetchedAt: number
}

async function fetchPositions(): Promise<PositionRecord[]> {
  const r = await fetch('/api/positions')
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

export async function fetchLiveSnapshot(): Promise<LiveSnapshot> {
  const { since, hoursSinceMidnight } = todayRange()

  const [equity, wallet, statisticsToday, positions, newsPipeline, health] = await Promise.all([
    settle(fetchEquity(hoursSinceMidnight)),
    settle(fetchWalletBalance()),
    settle(fetchStatistics(since, null)),
    settle(fetchPositions()),
    settle(fetchNewsPipeline(NEWS_PIPELINE_LIMIT)),
    settle(fetchHealthDetail()),
  ])

  const allCards = newsPipeline ?? []
  const newsPipelineToday = allCards.filter((c) => c.news.received_at >= since)

  return {
    equity,
    wallet,
    statisticsToday,
    positions,
    newsPipelineToday,
    newsPipelineTruncated: allCards.length >= NEWS_PIPELINE_LIMIT,
    health,
    fetchedAt: Date.now() / 1000,
  }
}
