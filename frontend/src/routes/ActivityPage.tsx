/**
 * Activity page — a shell with two sub-tabs. "Runs" (the old placeholder
 * name) became Activity; positions/fills + news are the genuinely
 * cross-section data that has no home in a section inspector. Overview
 * (P&L stat cards + equity curve) used to live here as a third sub-tab;
 * it's now its own top-level page.
 *
 *   Positions  — the fill ledger + materialized positions
 *   News       — news history + analysis         (placeholder, Phase 3)
 */
import { NavLink, Outlet } from 'react-router-dom'
import { usePageTitle } from '../lib/usePageTitle'

const subTabClass = ({ isActive }: { isActive: boolean }) =>
  [
    'px-3 py-1 rounded text-sm transition-colors',
    isActive
      ? 'bg-neutral-800 text-neutral-100'
      : 'text-neutral-400 hover:text-neutral-100 hover:bg-neutral-900',
  ].join(' ')

export function ActivityPage() {
  usePageTitle('Activity')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-6 pt-5 pb-3 flex items-baseline gap-4">
        <h1 className="text-lg font-medium text-neutral-100">Activity</h1>
        <nav className="flex gap-1">
          {/* no `end`: stays active for the /activity/positions/:id detail route */}
          <NavLink to="/activity/positions" className={subTabClass}>
            Positions
          </NavLink>
          <NavLink to="/activity/news" end className={subTabClass}>
            News
          </NavLink>
        </nav>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <Outlet />
      </div>
    </div>
  )
}
