/**
 * Activity › Position detail — one position's order book chart + the LLM's
 * reason for opening it. Reached by clicking a row in the Positions tab
 * (/activity/positions/:positionId).
 *
 * PD2/PD3 augment the backend response with `market_question` (catalog
 * lookup) and `analyzer_decisions` (analyzer_log lookup by news_id). PD5
 * renders both in the header / a dedicated rationale block. Per OD6, both
 * fields ride with the rest of `DetailData` through `frozenRef`, so a
 * closed position page never re-flickers when catalog / analyzer log
 * evicts the source data underneath.
 */
import { useRef } from 'react'
import { Link, useParams } from 'react-router-dom'
import { formatRelativeAgo, formatUTC } from '../../sections/news_source/time'
import { AnalyzerRationaleBlock } from './AnalyzerRationale'
import { OrderBookChart } from './OrderBookChart'
import { fetchOrderBookHistory, type OrderBookHistory } from './orderBookClient'
import { formatPnl, pnlClass } from './format'
import type { PositionRecord } from './portfolioTypes'
import { usePoll } from './usePoll'

async function fetchPosition(id: string): Promise<PositionRecord | null> {
  const r = await fetch(`/api/positions/${encodeURIComponent(id)}`)
  if (r.status === 404) return null
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionRecord
}

type DetailData = {
  position: PositionRecord | null
  history: OrderBookHistory | null
}

export function PositionDetail() {
  const { positionId } = useParams<{ positionId: string }>()
  const frozenRef = useRef<DetailData | null>(null)
  const { data, status, error } = usePoll<DetailData>(async () => {
    if (frozenRef.current !== null) return frozenRef.current
    const position = await fetchPosition(positionId ?? '')
    if (position === null) return { position: null, history: null }
    const history = await fetchOrderBookHistory(
      position.token_id,
      position.opened_at,
      position.closed_at,
    )
    const result: DetailData = { position, history }
    if (position.closed_at !== null) frozenRef.current = result
    return result
  })

  if (data === null) {
    return (
      <div className="grid place-items-center p-10">
        <p className="text-sm text-neutral-400">
          {status === 'error' ? `Backend unreachable: ${error}` : 'Loading…'}
        </p>
      </div>
    )
  }

  if (data.position === null) {
    return (
      <div className="px-6 py-10 flex flex-col gap-3">
        <Link to="/activity/positions" className="text-xs text-blue-400">
          ‹ Positions
        </Link>
        <p className="text-sm text-neutral-400">Position not found.</p>
      </div>
    )
  }

  const p = data.position
  const snapshots = data.history?.snapshots ?? []
  const exitPrice =
    p.closed_at !== null && p.realized_pnl !== null
      ? p.avg_entry_price + p.realized_pnl / p.qty
      : null

  return (
    <div className="px-6 pb-6 flex flex-col gap-4">
      <Link to="/activity/positions" className="text-xs text-blue-400">
        ‹ Positions
      </Link>

      {/* Header row: id, market identity, side, qty/price, status, PnL */}
      <div className="flex items-baseline gap-3 flex-wrap font-mono text-[12px]">
        <span className="text-neutral-100 font-semibold">#{p.id}</span>
        {/* PD2: market question, with condition_id truncation as fallback.
            Linked to the real Polymarket page when the market is still in
            the live catalog (question and url share the same lookup, so
            one is available iff the other is). */}
        {p.market_question ? (
          p.polymarket_url ? (
            <a
              href={p.polymarket_url}
              target="_blank"
              rel="noreferrer"
              className="text-sky-400 hover:text-sky-300 underline truncate max-w-[60ch]"
              title={`${p.market_question}\n\nmarket_id: ${p.market_id}\ncondition_id: ${p.condition_id}`}
            >
              {p.market_question}
            </a>
          ) : (
            <span
              className="text-neutral-200 truncate max-w-[60ch]"
              title={`${p.market_question}\n\nmarket_id: ${p.market_id}\ncondition_id: ${p.condition_id}`}
            >
              {p.market_question}
            </span>
          )
        ) : (
          <span
            className="text-neutral-500"
            title={`market_id: ${p.market_id}\ncondition_id: ${p.condition_id}\n(question unavailable — market evicted from catalog)`}
          >
            {p.market_id.slice(0, 18)}…
          </span>
        )}
        <span className={p.side === 'yes' ? 'text-emerald-300' : 'text-sky-300'}>
          {p.side.toUpperCase()}
        </span>
        <span className="text-neutral-300">
          {p.qty.toFixed(2)} @ {p.avg_entry_price.toFixed(3)}
        </span>
        <span
          className={p.status === 'open' ? 'text-amber-300' : 'text-neutral-400'}
        >
          {p.status}
        </span>
        {p.status === 'closed' && p.close_reason !== null && (
          <span className="text-neutral-500 text-[11px]">
            ({p.close_reason})
          </span>
        )}
        {p.realized_pnl !== null && (
          <span className={pnlClass(p.realized_pnl)}>
            {formatPnl(p.realized_pnl)}
          </span>
        )}
        <span
          className="ml-auto text-neutral-600 text-[10px]"
          title={formatUTC(p.opened_at)}
        >
          opened {formatRelativeAgo(p.opened_at)}
        </span>
        {p.closed_at !== null && (
          <span
            className="text-neutral-600 text-[10px]"
            title={formatUTC(p.closed_at)}
          >
            closed {formatRelativeAgo(p.closed_at)}
          </span>
        )}
      </div>

      {/* The news item that triggered this position's entry. Inline rather
         than a link — there's no per-item News route to link to. Omitted
         entirely for a paper/manual position (no news_id) or one that
         predates the news persistence rollout. */}
      {p.news && (
        <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-1.5">
          <div className="flex items-baseline gap-2 flex-wrap text-[11px]">
            <span className="text-neutral-400">Triggering news</span>
            <span className="px-1.5 py-0.5 text-[10px] uppercase font-mono rounded border bg-neutral-800 text-neutral-400 border-neutral-700/50">
              {p.news.urgency}
            </span>
            {p.news.sentiment !== null && (
              <span className="text-neutral-500">
                sentiment {p.news.sentiment}
              </span>
            )}
            <span
              className="ml-auto text-neutral-600"
              title={formatUTC(p.news.published_at)}
            >
              {formatRelativeAgo(p.news.published_at)}
            </span>
          </div>
          <div className="text-[12px] text-neutral-200 leading-relaxed whitespace-pre-wrap break-words">
            {p.news.content}
          </div>
        </div>
      )}

      {/* PD3+PD5: analyzer rationale block (LLM's stated reason for the
         decision). Empty list when no persisted analyzer_call row matches
         this position's news_id — rendered as "unavailable" rather than an
         error. */}
      <AnalyzerRationaleBlock decisions={p.analyzer_decisions ?? []} />

      {/* Exit-monitor decision that actually closed this position — richer
         than the coarse close_reason badge above (trigger detail,
         return_pct, peak_price). Omitted while open, or if closed before
         the exit_decision persistence rollout. */}
      {p.exit_decision && (
        <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-1.5">
          <div className="text-[11px] text-neutral-400">Exit</div>
          <div className="flex items-baseline gap-3 flex-wrap font-mono text-[12px]">
            {p.exit_decision.trigger !== null && (
              <span className="text-neutral-300">
                trigger{' '}
                <span className="text-neutral-100">
                  {p.exit_decision.trigger}
                </span>
              </span>
            )}
            {p.exit_decision.return_pct !== null && (
              <span className={pnlClass(p.exit_decision.return_pct)}>
                {(p.exit_decision.return_pct * 100).toFixed(2)}%
              </span>
            )}
            {p.exit_decision.peak_price !== null && (
              <span className="text-neutral-500">
                peak {p.exit_decision.peak_price.toFixed(3)}
              </span>
            )}
          </div>
          {p.exit_decision.reason && (
            <div className="text-[11px] text-neutral-400">
              {p.exit_decision.reason}
            </div>
          )}
        </div>
      )}

      <div className="rounded border border-neutral-800 p-3">
        {status === 'error' && (
          <div className="mb-2 rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
            Backend unreachable; data may be stale.
          </div>
        )}
        {snapshots.length === 0 ? (
          <div className="h-72 grid place-items-center text-[11px] text-neutral-500">
            No order book data for this position.
          </div>
        ) : (
          <OrderBookChart
            snapshots={snapshots}
            entry={{ ts: p.opened_at, price: p.avg_entry_price }}
            exit={
              p.closed_at !== null && exitPrice !== null
                ? { ts: p.closed_at, price: exitPrice }
                : null
            }
          />
        )}
      </div>
    </div>
  )
}

