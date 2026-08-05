/** Shared P&L formatting for the Activity tabs. */

export function formatPnl(n: number): string {
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(2)}`
}

export function pnlClass(n: number | null): string {
  if (n === null || n === 0) return 'text-neutral-400'
  return n > 0 ? 'text-emerald-300' : 'text-red-300'
}

// Percent return on cost basis (qty * avg_entry_price) — the same formula
// the exit monitor uses for its take-profit/stop-loss thresholds
// (threshold_v0.py: (current - entry) / entry === pnl / cost).
export function pnlPercent(pnl: number | null, cost: number): number | null {
  if (pnl === null || cost <= 0) return null
  return pnl / cost
}

export function formatPnlPercent(pct: number | null): string {
  if (pct === null) return ''
  const sign = pct < 0 ? '-' : '+'
  return `${sign}${Math.abs(pct * 100).toFixed(1)}%`
}
