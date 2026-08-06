import { useState } from 'react'
import { usePageTitle } from '../lib/usePageTitle'
import { BacktestForm } from './backtest/BacktestForm'
import { BacktestResultDashboard } from './backtest/BacktestResultDashboard'
import type { BacktestResponse } from './backtest/backtestClient'

export function BacktestPage() {
  usePageTitle('Backtest')
  const [result, setResult] = useState<BacktestResponse | null>(null)

  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-4 sm:px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">Backtest</h1>
        <p className="mt-1 text-xs text-neutral-500 max-w-2xl">
          Replays persisted history through a candidate entry/exit config —
          reusing the analyzer&apos;s actual historical calls (not re-running
          the LLM) and the real order-book snapshots. Only one backtest can
          run at a time.
        </p>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto px-4 sm:px-6 pb-6 flex flex-col gap-4">
        <BacktestForm onResult={setResult} />
        {result && <BacktestResultDashboard data={result} />}
      </div>
    </div>
  )
}
