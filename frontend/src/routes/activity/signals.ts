/** Shared key/value rendering for a section's raw `signals` payload at
 * decision time. Shape varies by skip reason — edge/min_edge/spread/
 * held_price for the edge-threshold checks, heat_cap_usd/open_cost for
 * heat_cap, daily_pnl_usd/limit_usd for kill_daily_loss, etc. — so this
 * stays a flat dump rather than a bespoke per-field layout. Used by
 * NewsCard's per-stage entry line and PositionDetail's entry signals card.
 */

export function formatSignalValue(v: unknown): string {
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return String(v)
    return v.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
  }
  return String(v)
}

export function formatSignalEntries(
  obj: Record<string, unknown> | null | undefined,
  omitKeys: readonly string[] = [],
): string | null {
  if (!obj) return null
  const entries = Object.entries(obj).filter(([k]) => !omitKeys.includes(k))
  if (entries.length === 0) return null
  return entries.map(([k, v]) => `${k} ${formatSignalValue(v)}`).join(' · ')
}
