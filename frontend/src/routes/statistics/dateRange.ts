/**
 * Statistics page date-range state — one unified control instead of a free
 * date picker: quick presets (24H/7D/30D/All-time) or a specific past
 * calendar month. Pure logic, no React, so it's trivially testable and
 * reusable from both DateRangeControl and StatisticsDashboard.
 *
 * Calendar-month boundaries are computed in the browser's LOCAL timezone
 * (JS's multi-arg Date constructor resolves locally, DST-aware, by
 * construction) then converted to UTC epoch seconds — consistent with the
 * app's standing "UTC storage, local-time interaction" convention already
 * established by chartTimeFormat.ts for chart axes.
 */

export type PresetOption = { readonly label: string; readonly hours: number | null }

// hours: null means "All-time" — resolveRange sends neither since nor until.
export const PRESET_OPTIONS: readonly PresetOption[] = [
  { label: '24H', hours: 24 },
  { label: '7D', hours: 24 * 7 },
  { label: '30D', hours: 24 * 30 },
  { label: 'All-time', hours: null },
]

export const MONTH_NAMES = [
  'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
] as const

export type DateRange =
  | { kind: 'preset'; hours: number | null }
  | { kind: 'month'; year: number; month: number } // month: 0-11, JS Date convention

export const DEFAULT_RANGE: DateRange = { kind: 'preset', hours: PRESET_OPTIONS[2].hours } // 30D

/** This year plus `lookback` prior years, descending — a plain client-side
 * list (no backend round trip to discover "which years have data"). */
export function yearOptions(currentYear: number, lookback = 3): number[] {
  return Array.from({ length: lookback + 1 }, (_, i) => currentYear - i)
}

/**
 * Resolve a DateRange into the [since, until) epoch-second bounds to send
 * to the API. `until` is always EXCLUSIVE — see statistics.py's module
 * docstring for why (calendar months need a half-open interval to avoid
 * double-counting the boundary instant); applied uniformly to presets too
 * so there's one mental model rather than two.
 */
export function resolveRange(
  range: DateRange,
  nowMs: number = Date.now(),
): { since: number | null; until: number | null } {
  if (range.kind === 'preset') {
    if (range.hours === null) return { since: null, until: null }
    const until = nowMs / 1000
    return { since: until - range.hours * 3600, until }
  }
  const start = new Date(range.year, range.month, 1, 0, 0, 0, 0)
  // month+1 rolls into next year natively when month === 11 (JS Date
  // normalizes out-of-range month components) — no special-casing needed.
  const end = new Date(range.year, range.month + 1, 1, 0, 0, 0, 0)
  return { since: start.getTime() / 1000, until: end.getTime() / 1000 }
}
