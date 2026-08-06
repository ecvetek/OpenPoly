/**
 * "Today" for the Live page = calendar day in the browser's local timezone,
 * converted to UTC epoch seconds — same local-midnight-then-convert approach
 * `resolveRange`'s month mode uses in ../statistics/dateRange.ts, just for a
 * single day instead of a month. Recomputed on every poll tick (callers
 * don't memoize this), so the boundary rolls over naturally at midnight
 * without any special-casing.
 */

export type TodayRange = {
  /** Epoch seconds at local midnight — inclusive lower bound for /api/statistics. */
  since: number
  /** Hours elapsed since local midnight — feeds fetchEquity's window_hours so
   * the equity curve covers exactly today. Always >= (1 / 3600), never 0, so
   * a fresh-boot request just after midnight still asks for a valid window. */
  hoursSinceMidnight: number
}

export function todayRange(nowMs: number = Date.now()): TodayRange {
  const now = new Date(nowMs)
  const midnight = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0, 0)
  const since = midnight.getTime() / 1000
  const hoursSinceMidnight = Math.max((nowMs - midnight.getTime()) / 3_600_000, 1 / 3600)
  return { since, hoursSinceMidnight }
}
