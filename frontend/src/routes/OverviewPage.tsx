/**
 * Overview — P&L stat cards + equity curve. Used to live as Activity's
 * default sub-tab; promoted to its own top-level page so it's visible
 * without a click into Activity.
 */
import { usePageTitle } from '../lib/usePageTitle'
import { OverviewTab } from './activity/OverviewTab'

export function OverviewPage() {
  usePageTitle('Overview')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">Overview</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <OverviewTab />
      </div>
    </div>
  )
}
