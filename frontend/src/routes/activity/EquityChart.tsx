/**
 * Equity curve — a lightweight-charts baseline series with the baseline at 0
 * (green above, red below). Chart chrome (construction, crosshair tooltip
 * wiring, de-dup-by-second) lives in useLightweightChart.ts, shared with
 * PnlCurveChart.tsx; this component only supplies the value per point and
 * the tooltip content.
 */
import type { UTCTimestamp } from 'lightweight-charts'
import { localCrosshairTimeFormatter } from './chartTimeFormat'
import type { EquityPoint } from './equityClient'
import { useLightweightChart } from './useLightweightChart'

function formatUsd(n: number): string {
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(2)}`
}

function renderTooltip(pt: EquityPoint, time: UTCTimestamp): string {
  return (
    `<div style="color:#8b949e">${localCrosshairTimeFormatter(time)}</div>` +
    `<div>Equity <b>${formatUsd(pt.equity)}</b></div>` +
    `<div style="color:#8b949e">realized ${formatUsd(pt.realized)} · ` +
    `unrealized ${formatUsd(pt.unrealized)}</div>`
  )
}

export function EquityChart({ points }: { points: EquityPoint[] }) {
  const { containerRef, tooltipRef } = useLightweightChart(points, (p) => p.equity, renderTooltip)

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
