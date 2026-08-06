/**
 * Live — a full-bleed, auto-refreshing TV/bigscreen dashboard: what the bot
 * did today (news, AI calls, positions opened/closed/won/lost), current
 * wallet balance and P&L, and a live pipeline visualization. Unlike the
 * other pages, there's nothing to click — it's built to be glanced at from
 * across a room.
 */
import { usePageTitle } from '../lib/usePageTitle'
import { LiveDashboard } from './live/LiveDashboard'

export function LivePage() {
  usePageTitle('Live')
  return (
    <div className="h-full flex flex-col bg-neutral-950">
      <LiveDashboard />
    </div>
  )
}
