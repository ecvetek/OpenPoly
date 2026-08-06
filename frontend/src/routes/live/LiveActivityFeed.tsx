/**
 * Live activity feed — a condensed, one-line-per-event ticker of today's
 * news → embedding → analyzer → entry pipeline, newest first. This is the
 * TV-glance counterpart to NewsCard.tsx's fully-expanded detail card: same
 * underlying NewsPipelineCard join, far less ink per row so a dozen events
 * fit in view at once.
 */
import { formatRelativeAgo } from '../../sections/news_source/time'
import type { CardState, NewsPipelineCard } from '../activity/newsTypes'

const MAX_ROWS = 20

const STATE_TONE: Record<CardState, string> = {
  filled: 'bg-emerald-900/40 text-emerald-300 border-emerald-700/50',
  skipped: 'bg-neutral-800 text-neutral-400 border-neutral-700/50',
  errored: 'bg-red-900/40 text-red-300 border-red-700/50',
  pending: 'bg-amber-900/40 text-amber-300 border-amber-700/50',
}

const URGENCY_DOT: Record<string, string> = {
  high: 'bg-red-400',
  medium: 'bg-amber-400',
  low: 'bg-neutral-600',
  regular: 'bg-neutral-600',
}

function stageSummary(card: NewsPipelineCard): string {
  const parts: string[] = []
  if (card.embedding) parts.push(`embed:${card.embedding.verdict}`)
  if (card.analyzer) parts.push(`analyze:${card.analyzer.verdict}`)
  if (card.entry) parts.push(`entry:${card.entry.fill_status ?? card.entry.verdict}`)
  return parts.join(' · ')
}

export function LiveActivityFeed({ cards }: { cards: NewsPipelineCard[] }) {
  const rows = cards.slice(0, MAX_ROWS)
  return (
    <div className="rounded border border-neutral-800 flex flex-col min-h-0 h-full">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500 px-3 pt-3 pb-2 shrink-0">
        Activity feed · today
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-3 pb-3 flex flex-col gap-1.5">
        {rows.length === 0 ? (
          <div className="grid place-items-center flex-1 text-[11px] text-neutral-600">
            No news processed yet today.
          </div>
        ) : (
          rows.map((c) => (
            <div
              key={c.news.news_id}
              className="flex items-center gap-2 text-[12px] font-mono border-b border-neutral-900 pb-1.5 last:border-b-0"
            >
              <span
                className={`h-1.5 w-1.5 rounded-full shrink-0 ${
                  URGENCY_DOT[(c.news.urgency ?? 'regular').toLowerCase()] ?? URGENCY_DOT.regular
                }`}
              />
              <span className="text-neutral-500 shrink-0 w-14">
                {formatRelativeAgo(c.news.received_at)}
              </span>
              <span className="text-neutral-300 truncate flex-1 min-w-0">{c.news.content}</span>
              <span className="text-neutral-600 text-[10px] shrink-0 hidden md:inline">
                {stageSummary(c)}
              </span>
              <span
                className={`px-1.5 py-0.5 text-[9px] uppercase rounded border shrink-0 ${STATE_TONE[c.state]}`}
              >
                {c.state}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
