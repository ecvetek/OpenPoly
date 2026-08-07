import { useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  [
    'px-3 py-1 rounded transition-colors',
    isActive
      ? 'bg-neutral-800 text-neutral-100'
      : 'text-neutral-400 hover:text-neutral-100 hover:bg-neutral-900',
  ].join(' ')

const mobileNavLinkClass = ({ isActive }: { isActive: boolean }) =>
  [
    'px-3 py-2 rounded transition-colors',
    isActive
      ? 'bg-neutral-800 text-neutral-100'
      : 'text-neutral-400 hover:text-neutral-100 hover:bg-neutral-900',
  ].join(' ')

const NAV_ITEMS = [
  { to: '/strategy', label: 'Strategy' },
  { to: '/live', label: 'Live' },
  { to: '/overview', label: 'Overview' },
  { to: '/positions', label: 'Positions' },
  { to: '/news', label: 'News' },
  { to: '/statistics', label: 'Statistics' },
  { to: '/backtest', label: 'Backtest' },
  { to: '/health', label: 'Health' },
]

export function AppShell() {
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  const location = useLocation()

  // Close the mobile nav on navigation. Adjusted during render (React's
  // documented pattern for "reset state when a prop changes") rather than in
  // an effect, so a route change closes the nav in the same commit instead of
  // painting it open for one frame before a follow-up effect fires.
  const [prevPathname, setPrevPathname] = useState(location.pathname)
  if (location.pathname !== prevPathname) {
    setPrevPathname(location.pathname)
    setMobileNavOpen(false)
  }

  return (
    <div className="h-screen flex flex-col bg-neutral-950 text-neutral-100 overflow-hidden">
      <header className="border-b border-neutral-800 shrink-0">
        <div className="flex items-center gap-6 px-4 sm:px-6 h-12">
          <span className="font-medium tracking-tight">openPoly</span>

          <nav className="hidden sm:flex gap-1 text-sm">
            {NAV_ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to} className={navLinkClass}>
                {item.label}
              </NavLink>
            ))}
          </nav>

          <button
            type="button"
            onClick={() => setMobileNavOpen((v) => !v)}
            aria-expanded={mobileNavOpen}
            aria-label="Toggle navigation menu"
            className="sm:hidden ml-auto p-2 -mr-2 rounded text-neutral-400 hover:text-neutral-100 hover:bg-neutral-900"
          >
            <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5">
              {mobileNavOpen ? (
                <path d="M5 5l10 10M15 5L5 15" strokeLinecap="round" />
              ) : (
                <path d="M3 5h14M3 10h14M3 15h14" strokeLinecap="round" />
              )}
            </svg>
          </button>
        </div>

        {mobileNavOpen && (
          <nav className="sm:hidden flex flex-col gap-1 px-4 pb-3 text-sm border-t border-neutral-800">
            {NAV_ITEMS.map((item) => (
              <NavLink key={item.to} to={item.to} className={mobileNavLinkClass}>
                {item.label}
              </NavLink>
            ))}
          </nav>
        )}
      </header>
      <main className="flex-1 min-h-0">
        <Outlet />
      </main>
    </div>
  )
}
