/**
 * PnlCurveChart — cumulative realized P&L for closed positions in the
 * selected range, as a lightweight-charts baseline series (green above
 * zero, red below). Chart chrome is shared with EquityChart.tsx via
 * useLightweightChart.ts; this component only supplies the value per point
 * (cumulative_pnl — this page is realized-only by design, no mark-to-market)
 * and the tooltip content.
 */
import type { UTCTimestamp } from 'lightweight-charts'
import { localCrosshairTimeFormatter } from '../activity/chartTimeFormat'
import { useLightweightChart } from '../activity/useLightweightChart'
import type { PnlCurvePoint } from './statisticsClient'

function formatUsd(n: number): string {
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(2)}`
}

function renderTooltip(pt: PnlCurvePoint, time: UTCTimestamp): string {
  return (
    `<div style="color:#8b949e">${localCrosshairTimeFormatter(time)}</div>` +
    `<div>Cumulative P&L <b>${formatUsd(pt.cumulative_pnl)}</b></div>`
  )
}

export function PnlCurveChart({ points }: { points: PnlCurvePoint[] }) {
  const { containerRef, tooltipRef } = useLightweightChart(
    points,
    (p) => p.cumulative_pnl,
    renderTooltip,
  )

  return (
    <div className="relative h-64 w-full">
      <div ref={containerRef} className="h-full w-full" />
      <div
        ref={tooltipRef}
        className="pointer-events-none absolute z-10 hidden rounded border border-neutral-700 bg-neutral-900/95 px-2 py-1 text-[11px] text-neutral-200"
      />
    </div>
  )
}
