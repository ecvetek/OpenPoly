import { usePageTitle } from '../lib/usePageTitle'
import { StatisticsDashboard } from './statistics/StatisticsDashboard'

export function StatisticsPage() {
  usePageTitle('Statistics')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">Statistics</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <StatisticsDashboard />
      </div>
    </div>
  )
}
