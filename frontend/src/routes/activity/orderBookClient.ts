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

// A CLOB-backfilled point where local order-book sampling had already
// stopped — price only, no bid/ask band.
export type PricePoint = [ts: number, price: number]

export type PositionPriceHistory = {
  position_id: number
  token_id: string
  snapshots: OrderBookSnapshot[]
  price_points: PricePoint[]
  market_end_date: number | null
  market_resolved: boolean
  winning_side: 'yes' | 'no' | null
}

/** Price history for one position, spanning open through close and on to
 * the market's expiry/resolution (unlike fetchOrderBookHistory, which is
 * bounded by whatever `until` the caller passes). See
 * `get_position_price_history` in openpoly/api/portfolio_routes.py. */
export async function fetchPositionPriceHistory(
  positionId: number,
): Promise<PositionPriceHistory> {
  const r = await fetch(`/api/positions/${positionId}/price-history`)
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionPriceHistory
}
