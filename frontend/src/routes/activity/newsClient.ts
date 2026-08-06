// fetch + join layer for the News pipeline tab (v16).
//
// Pulls four endpoints in parallel, joins by news_id, and derives each
// card's terminal state. Non-news endpoint failures degrade gracefully
// (their stage becomes null on every card); a news endpoint failure
// throws so the caller can surface a list-level error rather than
// silently rendering an empty page.

import type {
  AnalyzerCallEntry,
  CardState,
  EmbeddingCall,
  EntryDecision,
  NewsItem,
  NewsPipelineCard,
} from './newsTypes'

// Caller may raise this via the News tab's limit selector (up to 1000,
// matching the backend's own NEWS_LIMIT_MAX in inspect_routes.py).
export const DEFAULT_NEWS_LIMIT = 25

// Accurate, `limit`-independent counts read off each raw response's `total`
// (news) / `total` (section logs) field — see inspect_routes.py's
// `inspect_news` and runtime_routes.py's `_count_since`. Distinct from the
// joined `cards` array's length, which is capped at whatever `limit` was
// requested.
export type NewsPipelineTotals = {
  news: number
  embedding: number
  analyzer: number
  entry: number
}

// Shared fetch-4-endpoints-and-join body, parameterized by the query string
// so both the News tab's plain `limit` fetch and the Live dashboard's
// `since`-scoped fetch go through one implementation.
async function fetchPipelineJoin(
  qs: string,
  signal?: AbortSignal,
): Promise<{ cards: NewsPipelineCard[]; totals: NewsPipelineTotals }> {
  const [newsRes, embRes, anRes, enRes] = await Promise.allSettled([
    fetchJson<{ news?: NewsItem[]; total?: number }>(`/api/inspect/news?${qs}`, signal),
    fetchJson<{ entries?: EmbeddingCall[]; total?: number }>(`/api/embedding/log?${qs}`, signal),
    fetchJson<{ entries?: AnalyzerCallEntry[]; total?: number }>(`/api/analyzer/log?${qs}`, signal),
    fetchJson<{ entries?: EntryDecision[]; total?: number }>(`/api/entry/log?${qs}`, signal),
  ])

  if (newsRes.status === 'rejected') throw newsRes.reason

  const news = ((newsRes.value?.news ?? []) as NewsItem[])
  const embByNews = indexNewest(extractEntries<EmbeddingCall>(embRes))
  const anByNews = indexNewest(extractEntries<AnalyzerCallEntry>(anRes))
  const enByNews = indexNewest(extractEntries<EntryDecision>(enRes))

  const cards = [...news]
    .sort((a, b) => b.received_at - a.received_at)
    .map((n): NewsPipelineCard => {
      const embedding = embByNews.get(n.news_id) ?? null
      const analyzer = anByNews.get(n.news_id) ?? null
      const entry = enByNews.get(n.news_id) ?? null
      return {
        news: n,
        embedding,
        analyzer,
        entry,
        state: deriveState(embedding, analyzer, entry),
      }
    })

  return {
    cards,
    totals: {
      news: newsRes.value?.total ?? news.length,
      embedding: totalOf(embRes),
      analyzer: totalOf(anRes),
      entry: totalOf(enRes),
    },
  }
}

export async function fetchNewsPipeline(
  newsLimit: number = DEFAULT_NEWS_LIMIT,
  signal?: AbortSignal,
): Promise<NewsPipelineCard[]> {
  const { cards } = await fetchPipelineJoin(`limit=${newsLimit}`, signal)
  return cards
}

// Live dashboard variant — same join, plus `since` (epoch seconds) and the
// accurate, un-capped totals needed for "N news / AI calls today" tiles.
export async function fetchNewsPipelineSince(
  since: number,
  limit: number = DEFAULT_NEWS_LIMIT,
  signal?: AbortSignal,
): Promise<{ cards: NewsPipelineCard[]; totals: NewsPipelineTotals }> {
  return fetchPipelineJoin(`since=${since}&limit=${limit}`, signal)
}

// Newest-per-news_id wins. Section rings rarely duplicate (each section
// runs once per news tick), but `entry` retries are possible — keeping
// the most recent decision surfaces the current state, not history.
function indexNewest<T extends { news_id: string; ts: number }>(
  entries: T[],
): Map<string, T> {
  const m = new Map<string, T>()
  for (const e of entries) {
    const prev = m.get(e.news_id)
    if (!prev || e.ts > prev.ts) m.set(e.news_id, e)
  }
  return m
}

// Terminal-state precedence (most-specific wins). Only `fill_status ===
// 'filled'` proves the executor produced a real position — entry's own
// verdict 'ok' just means it emitted an OrderIntent; the executor may
// still have skipped (live_not_ready) or errored downstream.
//
//   filled      — entry produced a position
//   errored     — any stage hit an error or error-verdict, or any stage
//                 returned 'fail_open' (degraded, not a clean pass)
//   not_filled  — analyzer 'ok', but entry skipped (min_edge, spread, ...)
//                 or entry 'ok' with the executor refusing to fill
//   ai_rejected — embedding passed, analyzer's verdict was 'skip'
//   skipped     — embedding-stage skip only (no analyzer call happened)
//   pending     — news exists, nothing downstream yet
function deriveState(
  emb: EmbeddingCall | null,
  an: AnalyzerCallEntry | null,
  en: EntryDecision | null,
): CardState {
  if (en?.fill_status === 'filled') return 'filled'

  const anyError = Boolean(
    emb?.verdict === 'error' || emb?.error || emb?.verdict === 'fail_open' ||
    an?.verdict === 'error' || an?.error || an?.verdict === 'fail_open' ||
    en?.verdict === 'error' || en?.error || en?.verdict === 'fail_open',
  )
  if (anyError) return 'errored'

  const executorRefused =
    en?.verdict === 'ok' &&
    en?.fill_status != null &&
    en.fill_status !== 'filled'
  if (en != null && (en.verdict === 'skip' || executorRefused)) {
    return 'not_filled'
  }

  if (an != null && an.verdict === 'skip') return 'ai_rejected'

  if (emb?.verdict === 'skip') return 'skipped'

  return 'pending'
}

function extractEntries<T>(
  settled: PromiseSettledResult<{ entries?: T[] }>,
): T[] {
  if (settled.status !== 'fulfilled') return []
  return settled.value?.entries ?? []
}

// A rejected/degraded section log contributes 0 to its total rather than
// throwing — matches extractEntries' same non-news-endpoints-degrade-
// gracefully convention (see this file's top comment).
function totalOf(settled: PromiseSettledResult<{ total?: number }>): number {
  if (settled.status !== 'fulfilled') return 0
  return settled.value?.total ?? 0
}

async function fetchJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(url, { signal })
  if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`)
  return r.json() as Promise<T>
}
