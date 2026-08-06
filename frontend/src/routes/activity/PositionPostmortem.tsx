/**
 * Postmortem for a closed, losing position — hindsight on whether closing
 * was the right call, using price data collected after close through the
 * market's resolution (see PositionDetail's use of
 * `fetchPositionPriceHistory`, which keeps polling past `closed_at` for
 * exactly this).
 *
 * Two regimes:
 * - Market resolved cleanly (`market_resolved` + a definite `winning_side`):
 *   compares the actual realized P&L against the hypothetical P&L of
 *   holding to settlement (0 or 1 for the held side) — a definite verdict.
 * - Not yet resolved: shows the price range since close as a "here's what
 *   happened so far" signal, no verdict.
 */
import type { ReactNode } from 'react'
import { formatPnl, pnlClass } from './format'
import type { PositionPriceHistory } from './orderBookClient'
import type { PositionRecord } from './portfolioTypes'

export type PositionPostmortemProps = {
  position: PositionRecord
  priceHistory: PositionPriceHistory
}

function postClosePrices(position: PositionRecord, history: PositionPriceHistory): number[] {
  if (position.closed_at === null) return []
  const closedAt = position.closed_at
  const prices: number[] = []
  for (const s of history.snapshots) {
    if (s.recorded_at < closedAt) continue
    const bid = s.bids[0]?.[0]
    const ask = s.asks[0]?.[0]
    if (bid !== undefined && ask !== undefined) prices.push((bid + ask) / 2)
  }
  for (const [ts, price] of history.price_points) {
    if (ts >= closedAt) prices.push(price)
  }
  return prices
}

function Card({ children }: { children: ReactNode }) {
  return (
    <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-1.5">
      <div className="text-[11px] text-neutral-400">Postmortem</div>
      {children}
    </div>
  )
}

export function PositionPostmortem({ position, priceHistory }: PositionPostmortemProps) {
  if (position.status !== 'closed' || position.realized_pnl === null || position.realized_pnl >= 0) {
    return null
  }
  const exitPrice = position.avg_entry_price + position.realized_pnl / position.qty

  if (priceHistory.market_resolved) {
    if (priceHistory.winning_side === null) {
      return (
        <Card>
          <p className="text-[12px] text-neutral-300">
            Market resolved with a disputed/split outcome — no clean hold-to-settlement comparison.
          </p>
        </Card>
      )
    }
    const settlementPrice = priceHistory.winning_side === position.side ? 1 : 0
    const holdPnl = (settlementPrice - position.avg_entry_price) * position.qty
    const delta = holdPnl - position.realized_pnl
    const closingWasRight = delta <= 0
    return (
      <Card>
        <p className="text-[12px] text-neutral-300">
          Market resolved <span className="font-semibold text-neutral-100">{priceHistory.winning_side.toUpperCase()}</span> (
          settled at {settlementPrice.toFixed(0)}). Holding to settlement would have realized{' '}
          <span className={pnlClass(holdPnl)}>{formatPnl(holdPnl)}</span> vs. the{' '}
          <span className={pnlClass(position.realized_pnl)}>{formatPnl(position.realized_pnl)}</span> actually
          realized by closing at {exitPrice.toFixed(3)}.
        </p>
        <p className={`text-[12px] font-semibold ${closingWasRight ? 'text-emerald-300' : 'text-red-300'}`}>
          {closingWasRight
            ? `Closing early was right — it saved you ${formatPnl(Math.abs(delta))}.`
            : `Closing early cost you ${formatPnl(Math.abs(delta))} — holding would have won.`}
        </p>
      </Card>
    )
  }

  const prices = postClosePrices(position, priceHistory)
  if (prices.length === 0) {
    return (
      <Card>
        <p className="text-[12px] text-neutral-500">
          Market hasn't resolved yet, and no price data has come in since close.
        </p>
      </Card>
    )
  }
  const latest = prices[prices.length - 1]
  const high = Math.max(...prices)
  const low = Math.min(...prices)
  // For a YES holder, a higher price after close means holding would have
  // done better (and vice versa for NO) — the direction that makes closing
  // look wrong in hindsight, so far.
  const movedAgainstTheClose = position.side === 'yes' ? latest > exitPrice : latest < exitPrice
  return (
    <Card>
      <p className="text-[12px] text-neutral-300 font-mono">
        Since close: {low.toFixed(3)} – {high.toFixed(3)}, now {latest.toFixed(3)} (closed at{' '}
        {exitPrice.toFixed(3)}) — market hasn't resolved yet.
      </p>
      <p className="text-[12px] text-neutral-400">
        {movedAgainstTheClose
          ? 'Price has moved back toward — or past — your exit since you closed; waiting might still pay off.'
          : 'Price has kept moving away from your exit since you closed; closing looks right so far.'}
      </p>
    </Card>
  )
}
