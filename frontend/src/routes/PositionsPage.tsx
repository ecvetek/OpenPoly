/**
 * Positions — fill ledger + materialized positions. Used to live as
 * Activity's default sub-tab; promoted to its own top-level page (Activity
 * is gone — News was its only other sub-tab and is now top-level too).
 * Keeps an Outlet so the position-detail route can nest under here without
 * duplicating the header.
 */
import { usePageTitle } from '../lib/usePageTitle'
import { Outlet } from 'react-router-dom'

export function PositionsPage() {
  usePageTitle('Positions')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-4 sm:px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">Positions</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <Outlet />
      </div>
    </div>
  )
}
