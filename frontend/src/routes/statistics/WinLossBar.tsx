/**
 * WinLossBar — proportional-width bar of wins/losses/breakeven among the
 * closed-in-range positions. Hand-rolled (lightweight-charts has no
 * categorical/donut chart type) — plain divs sized via inline style, since
 * this codebase has no dynamic-width Tailwind class precedent (every
 * `w-[...]` hit elsewhere is a static pixel value).
 *
 * Shows counts only, not percentages — the bar's own proportional width
 * already conveys relative share visually. A numeric label here would be
 * `count / (wins + losses + breakeven)`, a different denominator than the
 * "Win rate" stat card (`wins / (wins + losses)`, breakeven excluded), so
 * printing both invites reading them as disagreeing win rates.
 */
const SEGMENTS: ReadonlyArray<{ key: 'wins' | 'losses' | 'breakeven'; label: string; className: string }> = [
  { key: 'wins', label: 'Win', className: 'bg-emerald-500' },
  { key: 'losses', label: 'Loss', className: 'bg-red-500' },
  { key: 'breakeven', label: 'Breakeven', className: 'bg-neutral-600' },
]

export function WinLossBar({
  wins,
  losses,
  breakeven,
}: {
  wins: number
  losses: number
  breakeven: number
}) {
  const counts = { wins, losses, breakeven }
  const total = wins + losses + breakeven

  if (total === 0) {
    return (
      <div className="rounded border border-neutral-800 px-3 py-6 text-center text-[11px] text-neutral-500">
        No closed positions in this range.
      </div>
    )
  }

  const present = SEGMENTS.filter((s) => counts[s.key] > 0)

  return (
    <div className="rounded border border-neutral-800 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[10px] uppercase tracking-wide text-neutral-500">Win / loss</div>
        <div className="text-[10px] text-neutral-500">share of all closed trades</div>
      </div>
      <div className="flex h-3 w-full overflow-hidden rounded-full bg-neutral-900">
        {present.map((s) => {
          const count = counts[s.key]
          const pct = (count / total) * 100
          return (
            <div
              key={s.key}
              className={s.className}
              style={{ width: `${pct}%` }}
              title={`${s.label}: ${count}`}
            />
          )
        })}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px]">
        {present.map((s) => {
          const count = counts[s.key]
          return (
            <div key={s.key} className="flex items-center gap-1.5">
              <span className={`inline-block h-2 w-2 rounded-full ${s.className}`} />
              <span className="text-neutral-400">
                {s.label} {count}
              </span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
