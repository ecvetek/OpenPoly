/** Statistics-page-specific formatters. Re-exports formatPnl/pnlClass from
 * activity/format rather than duplicating — those are generic P&L helpers,
 * not Activity-tab-specific. */
export { formatPnl, pnlClass, pnlPercent, formatPnlPercent } from '../activity/format'

export function formatPercent(n: number | null): string {
  return n === null ? '—' : `${(n * 100).toFixed(1)}%`
}

export function formatDuration(seconds: number | null): string {
  if (seconds === null) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const minutes = Math.floor(s / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ${minutes % 60}m`
  const days = Math.floor(hours / 24)
  return `${days}d ${hours % 24}h`
}
