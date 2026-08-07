/** Shared portfolio row types for the Activity tabs. */

/**
 * One verdict-ok analyzer call (LLM's stated reason for the decision).
 * Shape matches `_lookup_analyzer_decisions` in the backend (PD3).
 */
export type AnalyzerDecision = {
  rationale: string | null
  self_check: string | null
  p_model: number | null
  confidence: string | null
  ts: number
}

// The news item that triggered a position's entry — shown inline on
// PositionDetail rather than as a link (no per-item News route exists).
export type PositionNews = {
  content: string
  urgency: string
  sentiment: string | null
  published_at: number
}

// The exit-monitor decision that actually closed a position — richer than
// the coarse `close_reason` enum (trigger detail, return_pct, peak_price).
export type PositionExitDecision = {
  trigger: string | null
  return_pct: number | null
  fill_price: number | null
  realized_pnl: number | null
  reason: string | null
  peak_price: number | null
  ts: number
}

// The entry-section decision that opened a position — its raw `signals`
// dict at decision time (edge/spread/min_edge/max_spread/recent_move/...),
// shape varies by which gate produced it. Mirrors `_lookup_entry_decision`.
export type PositionEntryDecision = {
  signals: Record<string, unknown> | null
  reason: string | null
  ts: number
}

// One news/analyzer decision attached to a position. `relation` says how it
// relates: `opening` opened it, `reinforce` argued the same side again and was
// blocked by the one-position-per-(market, side) rule, `contradict` argued the
// OTHER side of the same market and was blocked outright. `side` is the side
// that decision WANTED — for `contradict` that is not the side held.
export type NewsSignal = {
  news_id: string
  ts: number
  side: 'yes' | 'no'
  relation: 'opening' | 'reinforce' | 'contradict'
  p_model: number | null
  confidence: string | null
  // null when the news row was evicted or never persisted (the news sink is
  // write-behind and best-effort) — the decision still counts.
  content: string | null
  urgency: string | null
}

// The regime derived from a position's news signals. `contested` dominates
// `reinforced`. The confluence exit section keys its peak-drawdown threshold
// off `state`; anything opened before that feature shipped reads as `solo`.
export type Confluence = {
  state: 'solo' | 'reinforced' | 'contested'
  support: number
  against: number
}

export type PositionRecord = {
  id: number
  market_id: string
  side: 'yes' | 'no'
  token_id: string
  condition_id: string
  qty: number
  avg_entry_price: number
  status: 'open' | 'closed'
  opened_at: number
  closed_at: number | null
  close_reason: string | null
  realized_pnl: number | null
  // Both /api/positions list AND /api/positions/{id} now populate these
  // (v15 PR1). market_question is null when the market has been evicted
  // from the catalog; analyzer_decisions is [] (never undefined) when the
  // analyzer_log ring no longer holds the original call. Kept optional
  // for tolerance — older test fixtures or partial mocks may omit them.
  market_question?: string | null
  analyzer_decisions?: AnalyzerDecision[]
  // Resolved from the live catalog — null when the market has been evicted.
  polymarket_url?: string | null
  // Market resolution date (epoch seconds), from the same catalog lookup as
  // market_question/polymarket_url — null under the same eviction fallback.
  market_end_date?: number | null
  // The news_id backing analyzer_decisions; null for a paper/manual
  // position with no news linkage. news is the full triggering item
  // (only populated on /api/positions/{id}, not the list route).
  news_id?: string | null
  news?: PositionNews | null
  // Only /api/positions/{id} populates this; null while open or if the
  // position closed before the exit_decision persistence rollout.
  exit_decision?: PositionExitDecision | null
  // Only /api/positions/{id} populates this; null if the position predates
  // entry_decision persistence (e.g. manual/paper open).
  entry_decision?: PositionEntryDecision | null
  // Polymarket event category tags (e.g. "crypto", "politics") from the
  // same catalog lookup as market_question; null when the market has been
  // evicted from the catalog. Only /api/positions/{id} populates this.
  market_tags?: string[] | null
  // Market liquidity context, same catalog lookup / eviction fallback as
  // market_tags. Only /api/positions/{id} populates these.
  market_volume_24h?: number | null
  market_liquidity?: number | null
  market_taker_fee_rate?: number | null
  // Mark-to-market P&L for an OPEN position (marked at the live level-1
  // bid) — the "if I closed this right now" number. Always null while
  // closed (use realized_pnl instead), and null if there's no live order
  // book yet for the token.
  unrealized_pnl?: number | null
  // Both routes populate `confluence`; only /api/positions/{id} populates the
  // full `news_signals` ledger, and only the list route carries the count.
  confluence?: Confluence | null
  news_signals?: NewsSignal[]
  news_signal_count?: number
}

// Response body of POST /api/positions/{id}/close — mirrors the backend's
// ExecResult. On filled=true, price/qty/position_id are set; on a skip,
// skip_reason carries a stable label instead.
export type CloseResult = {
  filled: boolean
  skip_reason?: string | null
  price?: number | null
  qty?: number | null
  position_id?: number | null
}

export type Fill = {
  id: number
  ts: number
  market_id: string
  side: 'yes' | 'no'
  action: 'buy' | 'sell'
  price: number
  qty: number
  fee: number
  position_id: number
  news_id: string | null
  trigger: string | null
}
