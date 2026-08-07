/**
 * CloseReasonBreakdown — compact per-close_reason counts over the
 * closed-in-range positions, sorted descending. Own local color map rather
 * than reaching into PositionCard.tsx's CLOSE_REASON_TONE (module-private
 * there, and shaped for badges — bg-*-900/40 + border — not a solid bar
 * fill).
 */
const REASON_COLOR: Record<string, string> = {
  take_profit: 'bg-emerald-500',
  stop_loss: 'bg-red-500',
  kill_switch: 'bg-orange-500',
  settlement: 'bg-sky-500',
  manual: 'bg-neutral-500',
  reconciled: 'bg-neutral-500',
  contested_exit: 'bg-amber-500',
}
const FALLBACK_COLOR = 'bg-neutral-700'

function reasonLabel(reason: string): string {
  return reason.replace(/_/g, ' ')
}

export function CloseReasonBreakdown({ breakdown }: { breakdown: Record<string, number> }) {
  const entries = Object.entries(breakdown).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((sum, [, count]) => sum + count, 0)

  return (
    <div className="rounded border border-neutral-800 p-3">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">
        Close reason
      </div>
      {entries.length === 0 ? (
        <div className="text-[11px] text-neutral-500">No closed positions in this range.</div>
      ) : (
        <div className="flex flex-col gap-1.5">
          {entries.map(([reason, count]) => {
            const pct = total === 0 ? 0 : (count / total) * 100
            return (
              <div key={reason} className="flex items-center gap-2">
                <div className="w-20 shrink-0 text-[11px] text-neutral-400 capitalize">
                  {reasonLabel(reason)}
                </div>
                <div className="flex-1 h-2 rounded-full bg-neutral-900 overflow-hidden">
                  <div
                    className={REASON_COLOR[reason] ?? FALLBACK_COLOR}
                    style={{ width: `${pct}%` }}
                  />
                </div>
                <div className="w-8 shrink-0 text-right text-[11px] font-mono text-neutral-500">
                  {count}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
