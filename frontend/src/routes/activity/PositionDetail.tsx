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
import { useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  formatLocalDateTime,
  formatRelativeAgo,
  formatTimeRemaining,
  formatUTC,
} from '../../sections/news_source/time'
import { AnalyzerRationaleBlock } from './AnalyzerRationale'
import { OrderBookChart } from './OrderBookChart'
import { fetchOrderBookHistory, type OrderBookHistory } from './orderBookClient'
import { formatPnl, pnlClass } from './format'
import { StatusBadge } from './PositionCard'
import type { CloseResult, PositionRecord } from './portfolioTypes'
import { usePoll } from './usePoll'

async function fetchPosition(id: string): Promise<PositionRecord | null> {
  const r = await fetch(`/api/positions/${encodeURIComponent(id)}`)
  if (r.status === 404) return null
  if (!r.ok) throw new Error(`HTTP ${r.status}`)
  return (await r.json()) as PositionRecord
}

async function closePosition(id: number): Promise<CloseResult> {
  const r = await fetch(`/api/positions/${id}/close`, { method: 'POST' })
  if (!r.ok) {
    const body = await r.json().catch(() => null)
    throw new Error(body?.detail ?? `HTTP ${r.status}`)
  }
  return (await r.json()) as CloseResult
}

type DetailData = {
  position: PositionRecord | null
  history: OrderBookHistory | null
}

export function PositionDetail() {
  const { positionId } = useParams<{ positionId: string }>()
  // Keyed on positionId, not just presence — a bare cached DetailData would
  // survive a future position-to-position navigation that doesn't remount
  // this component and show the previous position's frozen data under the
  // new id (unreachable today since every nav entry point remounts, but a
  // real latent bug for any future in-place link between positions).
  const frozenRef = useRef<{ positionId: string; data: DetailData } | null>(null)
  const { data, status, error, refetch } = usePoll<DetailData>(async () => {
    const pid = positionId ?? ''
    if (frozenRef.current !== null && frozenRef.current.positionId === pid) {
      return frozenRef.current.data
    }
    const position = await fetchPosition(pid)
    if (position === null) return { position: null, history: null }
    const history = await fetchOrderBookHistory(
      position.token_id,
      position.opened_at,
      position.closed_at,
    )
    const result: DetailData = { position, history }
    if (position.closed_at !== null) frozenRef.current = { positionId: pid, data: result }
    return result
  })
  const [closing, setClosing] = useState(false)
  const [closeStatus, setCloseStatus] = useState<string | null>(null)

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
  const cost = p.qty * p.avg_entry_price
  const sideTone = p.side === 'yes' ? 'text-emerald-300' : 'text-sky-300'

  async function onClosePosition() {
    if (closing) return
    if (!window.confirm(`Close position #${p.id} now at the level-1 bid?`)) return
    setClosing(true)
    setCloseStatus(null)
    try {
      const result = await closePosition(p.id)
      if (result.filled) {
        setCloseStatus(
          `Closed at ${result.price?.toFixed(3) ?? '?'} × ${result.qty?.toFixed(2) ?? '?'}.`,
        )
      } else {
        setCloseStatus(`Not closed: ${result.skip_reason ?? 'unknown reason'}.`)
      }
      refetch()
    } catch (e) {
      setCloseStatus(`Close failed: ${e instanceof Error ? e.message : String(e)}`)
    } finally {
      setClosing(false)
    }
  }

  return (
    <div className="px-6 pb-6 flex flex-col gap-4">
      <Link to="/activity/positions" className="text-xs text-blue-400">
        ‹ Positions
      </Link>

      {/* Header row: id + market identity only. Trading detail (side, size,
         cost, status, P&L, timestamps) lives in the Position card below,
         on its own line rather than crammed next to the title. */}
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
      </div>

      {/* Position card: side/size/price/cost/status/P&L/opened(+closed)/
         expiry — same fields and layout as a PositionCard row in the list,
         plus a manual close action while the position is open. */}
      <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-2">
        <div className="flex items-baseline gap-2 flex-wrap text-[11px]">
          <span className="text-neutral-400">Position</span>
          <StatusBadge status={p.status} closeReason={p.close_reason} />
        </div>

        <div className="flex items-baseline gap-4 flex-wrap font-mono text-[12px]">
          <span className={`font-semibold ${sideTone}`}>
            BUY_{p.side.toUpperCase()}
          </span>
          <span className="text-neutral-300">
            {p.qty.toFixed(2)} @ {p.avg_entry_price.toFixed(3)}
          </span>
          <span className="text-neutral-500">(${cost.toFixed(2)})</span>
          {p.status === 'open' && p.unrealized_pnl != null && (
            <span className={pnlClass(p.unrealized_pnl)}>
              {formatPnl(p.unrealized_pnl)}
            </span>
          )}
          <span
            className="ml-auto text-neutral-600 text-[10px]"
            title={formatUTC(p.opened_at)}
          >
            opened {formatRelativeAgo(p.opened_at)}
          </span>
        </div>

        {/* Market expiry — "<time remaining> / <exact resolution datetime>",
           flips to "expired / <datetime>" once the market's end_date has
           passed (the position itself may still be open pending settlement). */}
        {p.market_end_date != null && (
          <div
            className="text-[10px] text-neutral-600 font-mono"
            title={formatUTC(p.market_end_date)}
          >
            {formatTimeRemaining(p.market_end_date)} / {formatLocalDateTime(p.market_end_date)}
          </div>
        )}

        {p.closed_at !== null && exitPrice !== null && p.realized_pnl !== null && (
          <div className="flex items-baseline gap-4 flex-wrap font-mono text-[12px]">
            <span className={`font-semibold ${sideTone}`}>
              SELL_{p.side.toUpperCase()}
            </span>
            <span className="text-neutral-300">
              {p.qty.toFixed(2)} @ {exitPrice.toFixed(3)}
            </span>
            <span className={pnlClass(p.realized_pnl)}>
              {formatPnl(p.realized_pnl)}
            </span>
            <span
              className="ml-auto text-neutral-600 text-[10px]"
              title={formatUTC(p.closed_at)}
            >
              closed {formatRelativeAgo(p.closed_at)}
            </span>
          </div>
        )}

        {p.status === 'open' && (
          <div className="flex items-center gap-2 pt-1">
            {closeStatus && (
              <span className="text-[10px] text-neutral-500">{closeStatus}</span>
            )}
            <button
              type="button"
              disabled={closing}
              onClick={() => void onClosePosition()}
              className="ml-auto px-2 py-1 text-[11px] rounded border border-red-800 bg-red-900/30 hover:bg-red-900/50 text-red-200 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {closing ? 'Closing…' : 'Close position'}
            </button>
          </div>
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

