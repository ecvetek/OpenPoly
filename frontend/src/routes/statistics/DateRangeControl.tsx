/**
 * DateRangeControl — preset buttons (24H/7D/30D/All-time) styled like
 * OverviewTab.tsx's window-selector row, plus a Month/Year <select> pair for
 * picking a specific past calendar month. Fully controlled, no local state:
 * the displayed month/year derive from `value` when it's already in month
 * mode, else fall back to today's month/year — one source of truth (the
 * DateRange passed down from StatisticsDashboard), not a second state
 * variable shadowing it.
 */
import { MONTH_NAMES, PRESET_OPTIONS, type DateRange, yearOptions } from './dateRange'

const ACTIVE_BTN = 'bg-blue-600 text-white'
const INACTIVE_BTN = 'border border-neutral-700 text-neutral-400'
const SELECT_CLASS = 'rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-[11px] text-neutral-300'

export function DateRangeControl({
  value,
  onChange,
}: {
  value: DateRange
  onChange: (next: DateRange) => void
}) {
  const now = new Date()
  const displayYear = value.kind === 'month' ? value.year : now.getFullYear()
  const displayMonth = value.kind === 'month' ? value.month : now.getMonth()
  const monthActive = value.kind === 'month'

  return (
    <div className="flex items-center gap-3 flex-wrap">
      <div className="flex items-center gap-1 text-[10px] text-neutral-500">
        <span>range</span>
        {PRESET_OPTIONS.map((opt) => (
          <button
            key={opt.label}
            type="button"
            onClick={() => onChange({ kind: 'preset', hours: opt.hours })}
            className={`rounded px-1.5 py-0.5 ${
              value.kind === 'preset' && value.hours === opt.hours ? ACTIVE_BTN : INACTIVE_BTN
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>
      <div
        className={`flex items-center gap-1 rounded px-1 py-0.5 ${monthActive ? 'ring-1 ring-blue-600' : ''}`}
      >
        <select
          aria-label="Month"
          value={displayMonth}
          onChange={(e) => onChange({ kind: 'month', year: displayYear, month: Number(e.target.value) })}
          className={SELECT_CLASS}
        >
          {MONTH_NAMES.map((label, i) => (
            <option key={label} value={i}>
              {label}
            </option>
          ))}
        </select>
        <select
          aria-label="Year"
          value={displayYear}
          onChange={(e) => onChange({ kind: 'month', year: Number(e.target.value), month: displayMonth })}
          className={SELECT_CLASS}
        >
          {yearOptions(now.getFullYear()).map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>
      </div>
    </div>
  )
}
