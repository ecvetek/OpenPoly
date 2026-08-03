import { usePageTitle } from '../lib/usePageTitle'
import { HealthDashboard } from './health/HealthDashboard'

export function HealthPage() {
  usePageTitle('Health')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">Health</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <HealthDashboard />
      </div>
    </div>
  )
}
