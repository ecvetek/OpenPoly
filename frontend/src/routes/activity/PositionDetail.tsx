/**
 * Position detail — one position's price chart + the LLM's reason for
 * opening it. Reached by clicking a row in the Positions page
 * (/positions/:positionId).
 *
 * PD2/PD3 augment the backend response with `market_question` (catalog
 * lookup) and `analyzer_decisions` (analyzer_log lookup by news_id). PD5
 * renders both in the header / a dedicated rationale block.
 *
 * The chart keeps polling (via `fetchPositionPriceHistory`) past a
 * position's own close — through the market's actual resolution, not just
 * `closed_at` — so a closed trade can be judged in hindsight (see
 * `PositionPostmortem`). Only once the market has cleanly resolved does
 * `frozenRef` take over and stop polling for good.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  formatLocalDateTime,
  formatRelativeAgo,
  formatTimeRemaining,
  formatUTC,
} from '../../sections/news_source/time'
import { NewsConfluenceBlock } from './NewsConfluence'
import { OrderBookChart } from './OrderBookChart'
import {
  fetchPositionPriceHistory,
  type PositionPriceHistory,
  type PriceHistoryWindow,
} from './orderBookClient'
import { formatPnl, formatPnlPercent, pnlClass, pnlPercent } from './format'
import { StatusBadge } from './PositionCard'
import { PositionPostmortem } from './PositionPostmortem'
import type { CloseResult, NewsSignal, PositionRecord } from './portfolioTypes'
import { formatSignalEntries } from './signals'
import { usePoll } from './usePoll'

// Chart timeframe selector — mirrors OverviewTab.tsx's WINDOW_OPTIONS button
// row and the backend's PRICE_HISTORY_WINDOW_OPTIONS safelist
// (openpoly/api/portfolio_routes.py). Uppercase labels per the Polymarket
// reference this redesign followed; wire values stay lowercase (REST/param
// convention, zero backend case-folding needed) — the two are decoupled on
// purpose, this is a different widget than Overview's equity selector.
const WINDOW_OPTIONS: ReadonlyArray<{
  readonly label: string
  readonly value: PriceHistoryWindow
}> = [
  { label: '1H', value: '1h' },
  { label: '6H', value: '6h' },
  { label: '1D', value: '1d' },
  { label: '1W', value: '1w' },
  { label: '1M', value: '1m' },
  { label: 'ALL', value: 'all' },
]

// No existing generic currency formatter to reuse — format.ts only has
// P&L-specific ones (signed, always 2 decimals).
function formatUsdCompact(n: number): string {
  if (n >= 1_000_000) return `$${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `$${(n / 1_000).toFixed(1)}k`
  return `$${n.toFixed(0)}`
}

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
  history: PositionPriceHistory | null
  // Always window='all' — feeds Postmortem only, decoupled from the chart's
  // own `history` fetch. Postmortem's "since close" stats must reflect the
  // *entire* post-close period regardless of whatever zoom the user picked
  // for the chart, or e.g. picking "1H" on a days-old closed position would
  // silently truncate the postmortem's numbers. `null` while the position is
  // open (Postmortem doesn't render) or not yet loaded.
  allHistory: PositionPriceHistory | null
}

export function PositionDetail() {
  const { positionId } = useParams<{ positionId: string }>()
  const [chartWindow, setChartWindow] = useState<PriceHistoryWindow>('1d')
  // Keyed on `${positionId}:${chartWindow}`, not just positionId — a
  // resolved market's CLOB history for any given window is permanently
  // fixed (settlement doesn't change historical candles), so caching
  // indefinitely per-window is correct, not a workaround. Without the
  // window in the key, switching timeframes on an already-"Concluded"
  // position would return whatever window happened to be active when it
  // first froze.
  const frozenRef = useRef<Map<string, DetailData>>(new Map())
  // Set right before a manual/forced refetch so the fetcher below skips the
  // frozen short-circuit for exactly that one call — otherwise, once frozen,
  // the postmortem's refresh button would be a permanent no-op.
  const bypassFreezeRef = useRef(false)
  const { data, status, error, refetch } = usePoll<DetailData>(
    async () => {
      const pid = positionId ?? ''
      const key = `${pid}:${chartWindow}`
      if (frozenRef.current.has(key) && !bypassFreezeRef.current) {
        return frozenRef.current.get(key)!
      }
      bypassFreezeRef.current = false
      const position = await fetchPosition(pid)
      if (position === null) return { position: null, history: null, allHistory: null }
      const history = await fetchPositionPriceHistory(position.id, chartWindow)
      const allHistory =
        position.status !== 'closed'
          ? null
          : chartWindow === 'all'
            ? history
            : await fetchPositionPriceHistory(position.id, 'all')
      const result: DetailData = { position, history, allHistory }
      // Freeze only once the market has actually resolved — a closed
      // position keeps polling past `closed_at` (the backend keeps
      // extending the window up to the market's expiry) so the
      // chart/postmortem can show what the market did afterward.
      if (history.market_resolved) frozenRef.current.set(key, result)
      return result
    },
    3000,
    chartWindow,
  )
  const forceRefresh = useCallback(() => {
    bypassFreezeRef.current = true
    refetch()
  }, [refetch])
  // Once the market resolves, take one more forced refresh (bypassing the
  // frozen cache) before flipping the postmortem into its final "Concluded"
  // state — a visible "one more refresh, then conclude" beat rather than
  // silently trusting whichever poll happened to first observe resolution.
  const [concluded, setConcluded] = useState(false)
  const finalizingRef = useRef(false)
  useEffect(() => {
    if (!data?.history?.market_resolved || concluded) return
    if (!finalizingRef.current) {
      finalizingRef.current = true
      forceRefresh()
      return
    }
    finalizingRef.current = false
    setConcluded(true)
  }, [data, concluded, forceRefresh])
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
      <div className="px-4 sm:px-6 py-10 flex flex-col gap-3">
        <Link to="/positions" className="text-xs text-blue-400">
          ‹ Positions
        </Link>
        <p className="text-sm text-neutral-400">Position not found.</p>
      </div>
    )
  }

  const p = data.position
  const resolvedMarker =
    data.history?.market_resolved && data.history.market_end_date != null
      ? { ts: data.history.market_end_date, winningSide: data.history.winning_side }
      : null
  const exitPrice =
    p.closed_at !== null && p.realized_pnl !== null
      ? p.avg_entry_price + p.realized_pnl / p.qty
      : null
  const cost = p.qty * p.avg_entry_price
  const sideTone = p.side === 'yes' ? 'text-emerald-300' : 'text-sky-300'
  // Omits side/p_model/held_price — already visible above (BUY_{SIDE}
  // badge, analyzer's p=, and the @ price entry line respectively).
  const entrySignalsText = p.entry_decision?.signals
    ? formatSignalEntries(p.entry_decision.signals, ['side', 'p_model', 'held_price'])
    : null
  // A position that predates the confluence/signals rollout has no
  // news_signals but may still have the old single `news` field (plus
  // whatever analyzer_decisions matched it) — synthesize a one-item ledger
  // from those so News Confluence still shows the triggering news instead
  // of silently dropping it now that the dedicated "Triggering news" card
  // is gone.
  const confluenceSignals: NewsSignal[] =
    p.news_signals && p.news_signals.length > 0
      ? p.news_signals
      : p.news
        ? [
            {
              news_id: p.news_id ?? '',
              ts: p.news.published_at,
              side: p.side,
              relation: 'opening',
              p_model: null,
              confidence: null,
              content: p.news.content,
              urgency: p.news.urgency,
              sentiment: p.news.sentiment,
              rationale: p.analyzer_decisions?.[0]?.rationale ?? null,
              self_check: p.analyzer_decisions?.[0]?.self_check ?? null,
            },
          ]
        : []

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
    <div className="px-4 sm:px-6 pb-6 flex flex-col gap-4">
      <Link to="/positions" className="text-xs text-blue-400">
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

      {/* Market identity extras: Polymarket event tags (category chips) +
         a compact liquidity/volume/fee readout, from the same catalog
         lookup as the header's market_question — absent under the same
         eviction fallback. Purely informational context for judging the
         entry signals below (e.g. a thin market makes a given edge less
         trustworthy), never shown elsewhere on this page. */}
      {(p.market_tags?.length ||
        p.market_volume_24h != null ||
        p.market_liquidity != null ||
        p.market_taker_fee_rate != null) && (
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex flex-wrap gap-1">
            {p.market_tags?.map((tag) => (
              <span
                key={tag}
                className="px-1.5 py-0.5 text-[10px] font-mono rounded border border-neutral-700/50 bg-neutral-900 text-neutral-500"
              >
                {tag}
              </span>
            ))}
          </div>
          <span className="text-[10px] text-neutral-600 font-mono">
            {[
              p.market_volume_24h != null
                ? `vol24h ${formatUsdCompact(p.market_volume_24h)}`
                : null,
              p.market_liquidity != null
                ? `liquidity ${formatUsdCompact(p.market_liquidity)}`
                : null,
              p.market_taker_fee_rate != null
                ? `fee ${(p.market_taker_fee_rate * 100).toFixed(0)}%`
                : null,
            ]
              .filter(Boolean)
              .join(' · ')}
          </span>
        </div>
      )}

      {/* Position + Entry, paired side by side on wide viewports — what was
         opened and why, at a glance, without scrolling past one to reach
         the other. */}
      <div
        className={`grid gap-4 ${entrySignalsText ? 'lg:grid-cols-2' : 'grid-cols-1'}`}
      >
        {/* Position card: side/size/price/cost/status/P&L/opened(+closed)/
         expiry — same fields and layout as a PositionCard row in the list.
         Two columns: trading detail on the left, timestamps + the manual
         close action stacked top-right so an open position (no SELL row)
         doesn't leave a tall empty gap under a bottom-pinned button. */}
        <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex items-start justify-between gap-4">
          <div className="flex flex-col gap-2 min-w-0">
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
                  {formatPnl(p.unrealized_pnl)} (
                  {formatPnlPercent(pnlPercent(p.unrealized_pnl, cost))})
                </span>
              )}
            </div>

            {/* Market expiry — "<time remaining> / <exact resolution datetime>",
             flips to "expired / <datetime>" once the market's end_date has
             passed (the position itself may still be open pending settlement).
             Prefers the price-history lookup (durable — resolves by
             condition_id even once the market is evicted from the live
             catalog) over the plain catalog lookup, which goes null in
             exactly that case. */}
            {(data.history?.market_end_date ?? p.market_end_date) != null && (
              <div
                className="text-[10px] text-neutral-600 font-mono"
                title={formatUTC(data.history?.market_end_date ?? p.market_end_date ?? 0)}
              >
                {formatTimeRemaining(
                  data.history?.market_end_date ?? p.market_end_date ?? 0,
                )}{' '}
                /{' '}
                {formatLocalDateTime(
                  data.history?.market_end_date ?? p.market_end_date ?? 0,
                )}
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
                  {formatPnl(p.realized_pnl)} (
                  {formatPnlPercent(pnlPercent(p.realized_pnl, cost))})
                </span>
              </div>
            )}

            {closeStatus && (
              <span className="text-[10px] text-neutral-500">{closeStatus}</span>
            )}
          </div>

          <div className="flex flex-col items-end gap-1.5 shrink-0">
            <span className="text-neutral-600 text-[10px]" title={formatUTC(p.opened_at)}>
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
            {p.status === 'open' && (
              <button
                type="button"
                disabled={closing}
                onClick={() => void onClosePosition()}
                className="px-2 py-1 text-[11px] rounded border border-red-800 bg-red-900/30 hover:bg-red-900/50 text-red-200 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {closing ? 'Closing…' : 'Close position'}
              </button>
            )}
          </div>
        </div>

        {/* Entry-section decision signals (edge/spread/min_edge/max_spread/
         recent_move/...) at the moment this position was opened — the same
         generic key:value dump NewsCard's "④ Entry" stage shows. Absent
         entirely for a manual/paper position or one that predates
         entry_decision persistence. */}
        {entrySignalsText && (
          <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-1">
            <div className="text-[11px] text-neutral-400">Entry</div>
            <div className="text-[11px] text-neutral-500 font-mono break-words">
              {entrySignalsText}
            </div>
            {p.entry_decision?.reason && (
              <div className="text-[11px] text-neutral-600">
                {p.entry_decision.reason}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Every news decision on this market that touched this position —
         the opening one plus anything later that reinforced/contradicted it
         — enriched with each item's urgency/sentiment and (collapsed by
         default) the analyzer's rationale, folding in what used to be two
         separate "Triggering news" and "Analyzer rationale" cards. Falls
         back to a synthesized single-item ledger (confluenceSignals, above)
         for a position that predates news-confluence persistence. */}
      <NewsConfluenceBlock signals={confluenceSignals} confluence={p.confluence} />

      {/* Exit-monitor decision that actually closed this position — richer
         than the coarse close_reason badge above (trigger detail,
         return_pct, peak_price) — paired with Postmortem, its natural
         counterpart, side by side on wide viewports. Omitted while open, or
         if closed before the exit_decision persistence rollout. */}
      {(p.exit_decision || data.allHistory) && (
        <div
          className={`grid gap-4 ${p.exit_decision && data.allHistory ? 'lg:grid-cols-2' : 'grid-cols-1'}`}
        >
          {p.exit_decision && (
            <div className="rounded border border-neutral-800 bg-neutral-950 p-3 flex flex-col gap-1.5">
              <div className="text-[11px] text-neutral-400">Exit</div>
              <div className="flex items-baseline gap-3 flex-wrap font-mono text-[12px]">
                {p.exit_decision.trigger !== null && (
                  <span className="text-neutral-300">
                    trigger{' '}
                    <span className="text-neutral-100">{p.exit_decision.trigger}</span>
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

          {/* Postmortem: for any closed position — compares what actually
             happened against holding to settlement (once resolved) or shows
             the price trend since close (while still pending). Always fed
             the 'all'-windowed history, independent of the chart's own
             timeframe selection below — see DetailData.allHistory. */}
          {data.allHistory && (
            <PositionPostmortem
              position={p}
              priceHistory={data.allHistory}
              onRefresh={forceRefresh}
              concluded={concluded}
            />
          )}
        </div>
      )}

      <div className="rounded border border-neutral-800 p-3 flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">
            Price
          </span>
          <div className="flex items-center gap-1 text-[10px] text-neutral-500">
            {WINDOW_OPTIONS.map((opt) => (
              <button
                key={opt.value}
                type="button"
                onClick={() => setChartWindow(opt.value)}
                className={`rounded px-1.5 py-0.5 ${
                  chartWindow === opt.value
                    ? 'bg-blue-600 text-white'
                    : 'border border-neutral-700 text-neutral-400'
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
        {status === 'error' && (
          <div className="mb-2 rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
            Backend unreachable; data may be stale.
          </div>
        )}
        {(data.history?.price_history.length ?? 0) === 0 ? (
          <div className="h-72 grid place-items-center text-[11px] text-neutral-500">
            No price data for this position.
          </div>
        ) : (
          <OrderBookChart
            priceHistory={data.history?.price_history ?? []}
            window={chartWindow}
            entry={{ ts: p.opened_at, price: p.avg_entry_price }}
            exit={
              p.closed_at !== null && exitPrice !== null
                ? { ts: p.closed_at, price: exitPrice }
                : null
            }
            resolved={resolvedMarker}
          />
        )}
      </div>
    </div>
  )
}
