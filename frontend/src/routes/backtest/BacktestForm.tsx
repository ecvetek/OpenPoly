/**
 * BacktestForm — date range + entry/exit impl+config pickers + Run button.
 * Unlike the Statistics page (fetch on mount / on range change), a backtest
 * is an explicit, potentially-slow POST action — nothing runs until the
 * operator clicks Run.
 */
import { useMemo, useState } from 'react'

import { DateRangeControl } from '../statistics/DateRangeControl'
import { DEFAULT_RANGE, resolveRange, type DateRange } from '../statistics/dateRange'
import { runBacktest, type BacktestResponse, type SectionSelection } from './backtestClient'
import { SectionConfigPicker } from './SectionConfigPicker'

export function BacktestForm({ onResult }: { onResult: (result: BacktestResponse) => void }) {
  const [range, setRange] = useState<DateRange>(DEFAULT_RANGE)
  const { since, until } = useMemo(() => resolveRange(range), [range])
  const [entry, setEntry] = useState<SectionSelection | null>(null)
  const [exitSel, setExitSel] = useState<SectionSelection | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // resolveRange's "All-time" preset resolves to {since: null, until: null}
  // — fine for /api/statistics (an unbounded live query), but the backtest
  // endpoint requires a bounded range (a replay walks indexed point-queries
  // over a specific window, not "the whole database").
  const boundedRange = since !== null && until !== null
  const canRun = boundedRange && entry !== null && exitSel !== null && !running

  const run = async () => {
    if (!canRun || since === null || until === null || entry === null || exitSel === null) return
    setRunning(true)
    setError(null)
    const result = await runBacktest({ since, until, entry, exit: exitSel })
    setRunning(false)
    if (result.status === 'error') {
      setError(result.error)
      return
    }
    onResult(result.data)
  }

  return (
    <div className="flex flex-col gap-4 rounded border border-neutral-800 p-4">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <DateRangeControl value={range} onChange={setRange} />
        <button
          type="button"
          onClick={() => void run()}
          disabled={!canRun}
          className={`rounded px-3 py-1.5 text-xs font-medium transition-colors ${
            canRun
              ? 'bg-emerald-600 text-white hover:bg-emerald-500'
              : 'bg-neutral-800 text-neutral-500 cursor-not-allowed'
          }`}
        >
          {running ? 'Running…' : 'Run backtest'}
        </button>
      </div>

      {!boundedRange && (
        <div className="rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-200">
          Pick a bounded range (not All-time) — a backtest replays a specific
          window of history, not the whole database.
        </div>
      )}

      {error && (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
          {error}
        </div>
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">Entry</div>
          <SectionConfigPicker sectionType="entry" value={entry} onChange={setEntry} />
        </div>
        <div>
          <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-2">Exit</div>
          <SectionConfigPicker sectionType="exit" value={exitSel} onChange={setExitSel} />
        </div>
      </div>
    </div>
  )
}
