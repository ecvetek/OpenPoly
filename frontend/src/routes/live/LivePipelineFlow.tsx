/**
 * Live pipeline flow — a static mirror of the strategy canvas's
 * news_source → embedding → analyzer → entry → exit chain, but as a
 * read-only "is it actually doing something" visual: one box per stage,
 * today's count, and a brief pulse when a stage's count ticks up between
 * polls.
 *
 * Every count is a true unbounded "since local midnight" number — not
 * derived from the (display-capped) activity-feed cards array, which would
 * silently undercount on a busy day. News/embedding/analyzer come from
 * newsClient.ts's fetchNewsPipelineSince totals; entry/exit come from
 * /api/statistics' positions_opened/positions_closed, which are already
 * true aggregate counts (see openpoly/portfolio/statistics.py) and a
 * better fit for "opened"/"closed" than trying to infer "filled" off a
 * capped join anyway.
 */
import { useEffect, useRef, useState } from 'react'

type Stage = 'news' | 'embedding' | 'analyzer' | 'entry' | 'exit'

const STAGE_ORDER: ReadonlyArray<{ key: Stage; label: string; sub: string }> = [
  { key: 'news', label: 'News', sub: 'received' },
  { key: 'embedding', label: 'Embedding', sub: 'processed' },
  { key: 'analyzer', label: 'Analyzer', sub: 'scored' },
  { key: 'entry', label: 'Entry', sub: 'opened' },
  { key: 'exit', label: 'Exit', sub: 'closed' },
]

function PipelineNode({
  label,
  sub,
  count,
  pulsing,
  isLast,
}: {
  label: string
  sub: string
  count: number
  pulsing: boolean
  isLast: boolean
}) {
  return (
    <div className="flex items-center gap-2 flex-1 min-w-0">
      <div
        className={`flex-1 min-w-0 rounded-lg border px-3 py-3 text-center transition-all duration-500 ${
          pulsing
            ? 'border-emerald-500 bg-emerald-900/30 shadow-[0_0_16px_rgba(52,211,153,0.35)]'
            : 'border-neutral-800 bg-neutral-900/40'
        }`}
      >
        <div className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</div>
        <div className="mt-1 text-3xl font-mono font-semibold text-neutral-100 tabular-nums">
          {count}
        </div>
        <div className="text-[10px] text-neutral-500">{sub}</div>
      </div>
      {!isLast && <span className="text-neutral-700 text-lg shrink-0">→</span>}
    </div>
  )
}

export function LivePipelineFlow({
  newsCount,
  embeddingCount,
  analyzerCount,
  openedToday,
  closedToday,
}: {
  newsCount: number
  embeddingCount: number
  analyzerCount: number
  openedToday: number
  closedToday: number
}) {
  const entryCount = openedToday
  const exitCount = closedToday
  const counts: Record<Stage, number> = {
    news: newsCount,
    embedding: embeddingCount,
    analyzer: analyzerCount,
    entry: entryCount,
    exit: exitCount,
  }

  const prevCounts = useRef<Partial<Record<Stage, number>>>({})
  const [pulsing, setPulsing] = useState<Set<Stage>>(new Set())

  // Deps are the plain per-stage numbers (not the `counts` object, which is a
  // fresh literal every render) so this only re-fires when a count actually
  // changes.
  useEffect(() => {
    const current: Record<Stage, number> = {
      news: newsCount,
      embedding: embeddingCount,
      analyzer: analyzerCount,
      entry: entryCount,
      exit: exitCount,
    }
    const increased = (Object.keys(current) as Stage[]).filter(
      (key) => prevCounts.current[key] !== undefined && current[key] > (prevCounts.current[key] ?? 0),
    )
    prevCounts.current = current
    if (increased.length === 0) return
    setPulsing(new Set(increased))
    const t = setTimeout(() => setPulsing(new Set()), 1200)
    return () => clearTimeout(t)
  }, [newsCount, embeddingCount, analyzerCount, entryCount, exitCount])

  return (
    <div className="rounded border border-neutral-800 p-4">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-3">
        Pipeline · today
      </div>
      <div className="flex items-stretch gap-1">
        {STAGE_ORDER.map((s, i) => (
          <PipelineNode
            key={s.key}
            label={s.label}
            sub={s.sub}
            count={counts[s.key]}
            pulsing={pulsing.has(s.key)}
            isLast={i === STAGE_ORDER.length - 1}
          />
        ))}
      </div>
    </div>
  )
}
