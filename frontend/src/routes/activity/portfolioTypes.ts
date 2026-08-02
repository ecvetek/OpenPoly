/** Shared portfolio row types for the Activity tabs. */

/**
 * One verdict-ok analyzer call (LLM's stated reason for the decision).
 * Shape matches `_lookup_analyzer_decisions` in the backend (PD3).
 */
export type AnalyzerDecision = {
  rationale: string | null
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
  // The news_id backing analyzer_decisions; null for a paper/manual
  // position with no news linkage. news is the full triggering item
  // (only populated on /api/positions/{id}, not the list route).
  news_id?: string | null
  news?: PositionNews | null
  // Only /api/positions/{id} populates this; null while open or if the
  // position closed before the exit_decision persistence rollout.
  exit_decision?: PositionExitDecision | null
  // Mark-to-market P&L for an OPEN position (marked at the live level-1
  // bid) — the "if I closed this right now" number. Always null while
  // closed (use realized_pnl instead), and null if there's no live order
  // book yet for the token.
  unrealized_pnl?: number | null
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
