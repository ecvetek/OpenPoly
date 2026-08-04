/**
 * News — news history + analysis. Used to live as Activity's second
 * sub-tab; promoted to its own top-level page alongside Positions now that
 * Activity has been removed.
 */
import { usePageTitle } from '../lib/usePageTitle'
import { NewsTab } from './activity/NewsTab'

export function NewsPage() {
  usePageTitle('News')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <div className="px-4 sm:px-6 pt-5 pb-3">
        <h1 className="text-lg font-medium text-neutral-100">News</h1>
      </div>
      <div className="flex-1 min-h-0 overflow-y-auto">
        <NewsTab />
      </div>
    </div>
  )
}
