/** Client + types for GET /api/inspect/order-books/{token_id} and
 * GET /api/positions/{id}/price-history. */

export type OrderBookSnapshot = {
  recorded_at: number
  bids: [number, number][]
  asks: [number, number][]
}

export type OrderBookHistory = {
  token_id: string
  count: number
  snapshots: OrderBookSnapshot[]
}

export async function fetchOrderBookHistory(
  tokenId: string,
  since: number,
  until: number | null,
): Promise<OrderBookHistory> {
  const params = new URLSearchParams({ since: String(since) })
  if (until !== null) params.set('until', String(until))
  const r = await fetch(
    `/api/inspect/order-books/${encodeURIComponent(tokenId)}?${params}`,
  )
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as OrderBookHistory
}

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
 * the market's expiry/resolution (unlike fetchOrderBookHistory, which is
 * bounded by whatever `until` the caller passes). Always CLOB-sourced — one
 * consistent density across the whole visible window, open or closed. See
 * `get_position_price_history` in openpoly/api/portfolio_routes.py. */
export async function fetchPositionPriceHistory(
  positionId: number,
  window: PriceHistoryWindow = 'all',
): Promise<PositionPriceHistory> {
  const r = await fetch(`/api/positions/${positionId}/price-history?window=${window}`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionPriceHistory
}
