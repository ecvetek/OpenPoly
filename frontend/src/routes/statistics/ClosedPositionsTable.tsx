/**
 * ClosedPositionsTable — compact trade log for the selected range: market,
 * side, entry/exit price, P&L, close reason, opened/closed times.
 * Newest-first (the order /api/statistics already returns). Th/Td helpers
 * copied from PositionsTab.tsx's FillsTable.
 *
 * Each row links to the existing /activity/positions/:id detail route. A
 * real <Link> lives in the market cell (keyboard/ctrl-click accessible) —
 * an <a> can't legally wrap a <tr> (only <td>/<th> are valid <tr> children)
 * — plus onClick+useNavigate on the <tr> for click-anywhere convenience.
 *
 * Exit price is derived client-side the same way PositionCard.tsx and
 * PositionDetail.tsx already (intentionally) duplicate it, rather than
 * introducing a shared helper only this third callsite would use.
 */
import type { ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { formatLocalDateTime } from '../../sections/news_source/time'
import { StatusBadge } from '../activity/PositionCard'
import type { PositionRecord } from '../activity/portfolioTypes'
import { formatPnl, pnlClass } from './format'

function exitPrice(p: PositionRecord): number | null {
  return p.closed_at !== null && p.realized_pnl !== null
    ? p.avg_entry_price + p.realized_pnl / p.qty
    : null
}

export function ClosedPositionsTable({
  positions,
  truncated,
}: {
  positions: PositionRecord[]
  truncated: boolean
}) {
  const navigate = useNavigate()

  if (positions.length === 0) {
    return (
      <div className="rounded border border-neutral-800 px-3 py-6 text-center text-[11px] text-neutral-500">
        No closed positions in this range.
      </div>
    )
  }

  return (
    <div className="rounded border border-neutral-800 overflow-x-auto">
      <table className="w-full text-[11px]">
        <thead>
          <tr className="text-left text-[10px] uppercase tracking-wide text-neutral-500 border-b border-neutral-800">
            <Th>Market</Th>
            <Th>Side</Th>
            <Th right>Entry</Th>
            <Th right>Exit</Th>
            <Th right>P&L</Th>
            <Th>Reason</Th>
            <Th>Opened</Th>
            <Th>Closed</Th>
          </tr>
        </thead>
        <tbody className="font-mono">
          {positions.map((p) => {
            const exit = exitPrice(p)
            return (
              <tr
                key={p.id}
                onClick={() => navigate(`/activity/positions/${p.id}`)}
                className="cursor-pointer border-b border-neutral-900 last:border-0 hover:bg-neutral-900/60"
              >
                <Td>
                  <Link
                    to={`/activity/positions/${p.id}`}
                    onClick={(e) => e.stopPropagation()}
                    className="text-neutral-300 hover:underline"
                    title={p.market_question ?? p.condition_id}
                  >
                    {p.market_question ?? `${p.condition_id.slice(0, 16)}…`}
                  </Link>
                </Td>
                <Td tone={p.side === 'yes' ? 'text-emerald-300' : 'text-sky-300'}>
                  {p.side.toUpperCase()}
                </Td>
                <Td right>{p.avg_entry_price.toFixed(3)}</Td>
                <Td right>{exit === null ? '—' : exit.toFixed(3)}</Td>
                <Td right tone={pnlClass(p.realized_pnl)}>
                  {p.realized_pnl === null ? '—' : formatPnl(p.realized_pnl)}
                </Td>
                <Td>
                  <StatusBadge status="closed" closeReason={p.close_reason} />
                </Td>
                <Td tone="text-neutral-500">{formatLocalDateTime(p.opened_at)}</Td>
                <Td tone="text-neutral-500">
                  {p.closed_at === null ? '—' : formatLocalDateTime(p.closed_at)}
                </Td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {truncated && (
        <div className="px-3 py-2 text-[10px] text-neutral-500 border-t border-neutral-800">
          Showing the 200 most recent — narrow the range to see more.
        </div>
      )}
    </div>
  )
}

function Th({ children, right }: { children: ReactNode; right?: boolean }) {
  return <th className={`px-3 py-1.5 font-medium ${right ? 'text-right' : ''}`}>{children}</th>
}

function Td({
  children,
  right,
  tone,
}: {
  children: ReactNode
  right?: boolean
  tone?: string
}) {
  return (
    <td className={`px-3 py-1.5 ${right ? 'text-right' : ''} ${tone ?? 'text-neutral-300'}`}>
      {children}
    </td>
  )
}
