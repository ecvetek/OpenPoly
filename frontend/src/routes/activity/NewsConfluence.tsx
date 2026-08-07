/**
 * NewsConfluenceBlock — the news that argued for and against a position, and
 * the risk regime that follows from it.
 *
 * A position holds one side of one market, so every later headline the
 * pipeline decides on that market is blocked from opening anything. Instead of
 * being discarded, each blocked decision attaches here: `reinforce` when it
 * wanted the side we hold, `contradict` when it wanted the other side. The
 * derived state is what `ConfluenceExitV0` keys its peak-drawdown threshold
 * off — `reinforced` gives the position room to ride out a retrace,
 * `contested` tightens the leash because a reversion is plausible.
 *
 * Rendered whenever a position has any signals. A position opened before this
 * shipped has an empty ledger and the block is omitted entirely rather than
 * showing a misleading "solo" badge over nothing.
 */
import { useState } from 'react'
import { formatRelativeAgo, formatUTC } from '../../sections/news_source/time'
import type { Confluence, NewsSignal } from './portfolioTypes'

const STATE_STYLE: Record<Confluence['state'], string> = {
  solo: 'bg-neutral-800 text-neutral-300 border-neutral-700/50',
  reinforced: 'bg-emerald-900/40 text-emerald-300 border-emerald-800/50',
  contested: 'bg-amber-900/40 text-amber-300 border-amber-800/50',
}

const STATE_BLURB: Record<Confluence['state'], string> = {
  solo: 'Only the opening news backs this position — the baseline drawdown threshold applies.',
  reinforced:
    'Multiple headlines agree with this position, so its peak-drawdown threshold is loosened (or off) to let it ride out a retrace. The stop-loss floor still applies.',
  contested:
    'A headline argued the opposite side of this market. A reversion is plausible, so the peak-drawdown threshold is tightened.',
}

const RELATION_STYLE: Record<NewsSignal['relation'], string> = {
  opening: 'bg-blue-900/40 text-blue-300 border-blue-800/50',
  reinforce: 'bg-emerald-900/40 text-emerald-300 border-emerald-800/50',
  contradict: 'bg-amber-900/40 text-amber-300 border-amber-800/50',
}

const RELATION_LABEL: Record<NewsSignal['relation'], string> = {
  opening: 'opened',
  reinforce: 'reinforced',
  contradict: 'contradicted',
}

/**
 * Compact regime badge for the positions list. Renders nothing for a position
 * with no signals at all — a `solo` badge over an empty ledger would suggest
 * the pipeline evaluated something when it never did.
 */
export function ConfluenceBadge({
  confluence,
  signalCount,
}: {
  confluence: Confluence | null | undefined
  signalCount: number | undefined
}) {
  if (!confluence || !signalCount) return null
  return (
    <span
      className={`px-1.5 py-0.5 text-[10px] uppercase font-mono rounded border ${STATE_STYLE[confluence.state]}`}
      title={`${confluence.support} news for / ${confluence.against} against this position`}
    >
      {confluence.state} {confluence.support}/{confluence.against}
    </span>
  )
}

function confidenceClass(confidence: string | null): string {
  if (confidence === 'high') return 'text-emerald-300'
  if (confidence === 'medium') return 'text-amber-300'
  return 'text-neutral-400'
}

// Case-insensitive: urgency is a loosely-typed free string upstream, not a
// fixed union, so unrecognized values fall back to the same neutral style
// the old "Triggering news" card used for every value regardless of level.
function urgencyClass(urgency: string): string {
  const u = urgency.toLowerCase()
  if (u === 'high') return 'bg-red-900/40 text-red-300 border-red-800/50'
  if (u === 'medium') return 'bg-amber-900/40 text-amber-300 border-amber-800/50'
  return 'bg-neutral-800 text-neutral-400 border-neutral-700/50'
}

function sentimentClass(sentiment: string): string {
  const s = sentiment.toLowerCase()
  if (s === 'positive' || s === 'bullish') return 'text-emerald-300'
  if (s === 'negative' || s === 'bearish') return 'text-red-300'
  return 'text-neutral-400'
}

export function NewsConfluenceBlock({
  signals,
  confluence,
}: {
  signals: NewsSignal[]
  confluence: Confluence | null | undefined
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  if (signals.length === 0) return null

  const state = confluence?.state ?? 'solo'

  function toggleExpanded(key: string) {
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-2">
      <div className="flex items-baseline gap-2 flex-wrap text-[11px]">
        <span className="text-neutral-400">News confluence</span>
        <span
          className={`px-1.5 py-0.5 text-[10px] uppercase font-mono rounded border ${STATE_STYLE[state]}`}
        >
          {state}
        </span>
        {confluence && (
          <span className="text-neutral-600 font-mono">
            {confluence.support} for / {confluence.against} against
          </span>
        )}
        <span className="ml-auto text-neutral-600">
          {signals.length} signal{signals.length === 1 ? '' : 's'}
        </span>
      </div>

      <div className="text-[11px] text-neutral-500 leading-relaxed">
        {STATE_BLURB[state]}
      </div>

      <div className="flex flex-col gap-1.5">
        {signals.map((s) => {
          const key = `${s.news_id}-${s.ts}-${s.relation}`
          const isExpanded = expanded.has(key)
          const hasReasoning = s.rationale !== null || s.self_check !== null
          return (
            <div
              key={key}
              className="rounded bg-neutral-900/60 p-2.5 flex flex-col gap-1"
            >
              <div className="flex items-baseline gap-2 flex-wrap text-[10px] font-mono">
                <span
                  className={`px-1.5 py-0.5 uppercase rounded border ${RELATION_STYLE[s.relation]}`}
                >
                  {RELATION_LABEL[s.relation]}
                </span>
                {s.urgency !== null && (
                  <span
                    className={`px-1.5 py-0.5 uppercase rounded border ${urgencyClass(s.urgency)}`}
                  >
                    {s.urgency}
                  </span>
                )}
                <span className="text-neutral-400 uppercase">wanted {s.side}</span>
                {s.p_model !== null && (
                  <span className="text-neutral-400">p={s.p_model.toFixed(2)}</span>
                )}
                {s.confidence !== null && (
                  <span className={confidenceClass(s.confidence)}>{s.confidence}</span>
                )}
                {s.sentiment !== null && (
                  <span className={sentimentClass(s.sentiment)}>{s.sentiment}</span>
                )}
                <span className="ml-auto text-neutral-600" title={formatUTC(s.ts)}>
                  {formatRelativeAgo(s.ts)}
                </span>
              </div>
              <div className="text-[12px] text-neutral-200 leading-relaxed whitespace-pre-wrap break-words">
                {s.content ?? (
                  <span className="text-neutral-500 italic">
                    News text unavailable — the item was never persisted or has since been
                    evicted. The decision itself still counts.
                  </span>
                )}
              </div>
              {hasReasoning && (
                <>
                  <button
                    type="button"
                    onClick={() => toggleExpanded(key)}
                    className="self-start text-[10px] text-blue-400 hover:text-blue-300"
                  >
                    {isExpanded ? 'Hide reasoning ▾' : 'Show reasoning ▸'}
                  </button>
                  {isExpanded && (
                    <div className="flex flex-col gap-1 border-t border-neutral-800/70 pt-1">
                      {s.rationale && (
                        <div className="text-[12px] text-neutral-200 leading-relaxed whitespace-pre-wrap break-words">
                          {s.rationale}
                        </div>
                      )}
                      {s.self_check && (
                        <div className="text-[11px] text-neutral-500 leading-relaxed whitespace-pre-wrap break-words">
                          {s.self_check}
                        </div>
                      )}
                    </div>
                  )}
                </>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
