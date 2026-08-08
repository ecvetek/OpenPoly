/** Client + types for GET /api/positions/{id}/price-history. */

// A CLOB-sourced point on the unified price line — price only, no depth.
export type PricePoint = [ts: number, price: number]

export type PriceHistoryWindow = '1h' | '6h' | '1d' | '1w' | '1m' | 'all'

export type PositionPriceHistory = {
  position_id: number
  token_id: string
  window: string
  price_history: PricePoint[]
  market_end_date: number | null
  market_resolved: boolean
  winning_side: 'yes' | 'no' | null
}

/** Price history for one position, spanning open through close and on to
 * the market's expiry/resolution. Always CLOB-sourced — one consistent
 * density across the whole visible window, open or closed. See
 * `get_position_price_history` in openpoly/api/portfolio_routes.py. */
export async function fetchPositionPriceHistory(
  positionId: number,
  window: PriceHistoryWindow = 'all',
): Promise<PositionPriceHistory> {
  const r = await fetch(`/api/positions/${positionId}/price-history?window=${window}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionPriceHistory
}
