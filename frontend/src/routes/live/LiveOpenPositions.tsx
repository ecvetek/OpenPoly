/**
 * Live open-positions ticker — condensed rows (market, side, unrealized
 * P&L, opened-ago), newest first. The TV-glance counterpart to
 * PositionCard.tsx, which is built for click-through detail rather than
 * at-a-glance scanning.
 */
import { formatRelativeAgo } from '../../sections/news_source/time'
import { formatPnl, pnlClass } from '../activity/format'
import type { PositionRecord } from '../activity/portfolioTypes'

export function LiveOpenPositions({ positions }: { positions: PositionRecord[] }) {
  const open = positions
    .filter((p) => p.status === 'open')
    .slice()
    .sort((a, b) => b.opened_at - a.opened_at)

  return (
    <div className="rounded border border-neutral-800 flex flex-col min-h-0 h-full">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500 px-3 pt-3 pb-2 shrink-0">
        Open positions · {open.length}
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-3 pb-3 flex flex-col gap-1.5">
        {open.length === 0 ? (
          <div className="grid place-items-center flex-1 text-[11px] text-neutral-600">
            No open positions.
          </div>
        ) : (
          open.map((p) => (
            <div
              key={p.id}
              className="flex items-center gap-2 text-[12px] font-mono border-b border-neutral-900 pb-1.5 last:border-b-0"
            >
              <span className={`shrink-0 font-semibold ${p.side === 'yes' ? 'text-emerald-300' : 'text-sky-300'}`}>
                {p.side.toUpperCase()}
              </span>
              <span className="text-neutral-300 truncate flex-1 min-w-0">
                {p.market_question ?? p.condition_id.slice(0, 18)}
              </span>
              {p.unrealized_pnl != null && (
                <span className={`shrink-0 ${pnlClass(p.unrealized_pnl)}`}>
                  {formatPnl(p.unrealized_pnl)}
                </span>
              )}
              <span className="text-neutral-600 text-[10px] shrink-0 w-14 text-right">
                {formatRelativeAgo(p.opened_at)}
              </span>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
