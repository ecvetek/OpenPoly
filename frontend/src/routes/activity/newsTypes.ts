// Types for the News pipeline tab (v16). News carries through four
// stages, all backed by persisted DB tables (`news_item` /
// `embedding_call` / `analyzer_call` / `entry_decision`). The three
// non-news endpoints are fetched at the same `limit` as the news fetch
// itself (see newsClient.ts), so a stage in a `NewsPipelineCard` is null
// only when the news item genuinely hasn't reached that stage yet — not
// because of ring/eviction behavior.

// Matches openpoly.news.ring_buffer.Urgency. Tradingnews tends to send
// `regular`; `high`/`medium`/`low` are the bucketed view used by filters.
export type Urgency = 'high' | 'medium' | 'low' | 'regular'

// Matches openpoly.runtime.section_log.Verdict — universal across the
// three section logs (embedding / analyzer / entry).
export type Verdict = 'ok' | 'skip' | 'fail_open' | 'error'

// One persisted NewsItem from `/api/inspect/news` (returned as `news[]`
// inside a `{count, news}` envelope).
export type NewsItem = {
  id: number
  news_id: string
  content: string
  urgency: Urgency
  sentiment: string | null
  published_at: number
  received_at: number
}

// One row in `/api/embedding/log.entries[]`.
export type EmbeddingCall = {
  ts: number
  news_id: string
  news_content_preview: string
  urgency: string
  verdict: Verdict
  candidate_count: number
  top_market_id: string | null
  top_score: number | null
  catalog_size: number
  latency_ms: number
  error: string | null
  // Resolved from the live catalog at response time — null when the market
  // has since been evicted (closed / filtered), same as top_market_id being
  // unresolvable to a question elsewhere.
  top_market_polymarket_url: string | null
  top_market_question: string | null
}

// One row in `/api/analyzer/log.entries[]` — a superset of
// `AnalyzerDecision` from portfolioTypes.ts. AnalyzerRationaleBlock keeps
// using the narrower type (rationale-only view); NewsCard's analyzer
// stage uses this full shape (verdict / market_id / error matter for
// the timeline node, not for the rationale rollup).
export type AnalyzerCallEntry = {
  ts: number
  news_id: string
  news_content_preview: string
  urgency: string
  verdict: Verdict
  p_model: number | null
  confidence: string | null
  market_id: string | null
  latency_ms: number
  error: string | null
  rationale: string | null
  // Resolved from the live catalog at response time — null when the market
  // has since been evicted (closed / filtered).
  market_polymarket_url: string | null
  market_question: string | null
}

// One row in `/api/entry/log.entries[]`. `fill_*` and `position_id` are
// set only when entry's OrderIntent reached the executor and the
// executor responded; `verdict==ok` does not imply a fill (executor may
// still skip / error).
export type EntryDecision = {
  ts: number
  news_id: string
  ar_p_model: number | null
  ar_market_id: string | null
  verdict: Verdict
  side: string | null
  qty: number | null
  price: number | null
  reason: string | null
  latency_ms: number
  error: string | null
  fill_status: string | null
  fill_price: number | null
  fill_qty: number | null
  position_id: number | null
}

// Terminal state of one news's journey through the pipeline.
//   - filled:   entry produced a position (fill_status === 'filled')
//   - errored:  any stage errored
//   - skipped:  embedding / analyzer / entry skipped (and no later error)
//   - pending:  news exists but no downstream stage yet (or in-flight)
// Filter chip semantics (D6) build on this.
export type CardState = 'filled' | 'skipped' | 'errored' | 'pending'

// Join product — one per NewsItem (1:1 by news_id). Each non-news stage
// is null when the news item hasn't progressed there yet (e.g. analyzer
// null when embedding skipped/errored) — not because of any backend
// ring/eviction behavior; see newsClient.ts's top comment. UI must
// render each segment independently with a fallback — see plan
// §Risks/§3.
export type NewsPipelineCard = {
  news: NewsItem
  embedding: EmbeddingCall | null
  analyzer: AnalyzerCallEntry | null
  entry: EntryDecision | null
  state: CardState
}
