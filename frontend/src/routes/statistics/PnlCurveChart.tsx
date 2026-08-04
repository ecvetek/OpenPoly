/**
 * PnlCurveChart — cumulative realized P&L for closed positions in the
 * selected range, as a lightweight-charts baseline series (green above
 * zero, red below). Structural copy of EquityChart.tsx, simplified to one
 * value per point (cumulative_pnl) instead of three (equity/realized/
 * unrealized) — this page is realized-only by design, no mark-to-market.
 *
 * Same de-dup-by-second requirement as EquityChart (lightweight-charts
 * needs strictly-increasing whole-second timestamps) and same local-
 * timezone axis/crosshair labels (see chartTimeFormat.ts — the library's
 * UTCTimestamp axis is UTC-labeled by default despite the type name).
 */
import { useEffect, useRef } from 'react'
import {
  BaselineSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type UTCTimestamp,
} from 'lightweight-charts'
import { localCrosshairTimeFormatter, localTickMarkFormatter } from '../activity/chartTimeFormat'
import type { PnlCurvePoint } from './statisticsClient'

function formatUsd(n: number): string {
  const sign = n < 0 ? '-' : ''
  return `${sign}$${Math.abs(n).toFixed(2)}`
}

export function PnlCurveChart({ points }: { points: PnlCurvePoint[] }) {
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const bySecondRef = useRef<Map<number, PnlCurvePoint>>(new Map())

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { color: 'transparent' },
        textColor: '#8b949e',
        attributionLogo: true,
      },
      grid: {
        vertLines: { color: '#1f242c' },
        horzLines: { color: '#1f242c' },
      },
      localization: { timeFormatter: localCrosshairTimeFormatter },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        tickMarkFormatter: localTickMarkFormatter,
      },
      rightPriceScale: { borderColor: '#30363d' },
    })
    const series = chart.addSeries(BaselineSeries, {
      baseValue: { type: 'price', price: 0 },
      topLineColor: '#3fb950',
      topFillColor1: 'rgba(63,185,80,0.28)',
      topFillColor2: 'rgba(63,185,80,0.04)',
      bottomLineColor: '#f85149',
      bottomFillColor1: 'rgba(248,81,73,0.04)',
      bottomFillColor2: 'rgba(248,81,73,0.28)',
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
    })
    chart.subscribeCrosshairMove((param: MouseEventParams) => {
      const tip = tooltipRef.current
      if (!tip) return
      if (param.time === undefined || param.point === undefined) {
        tip.style.display = 'none'
        return
      }
      const pt = bySecondRef.current.get(param.time as number)
      if (!pt) {
        tip.style.display = 'none'
        return
      }
      tip.style.display = 'block'
      tip.style.left = `${param.point.x + 12}px`
      tip.style.top = `${param.point.y + 12}px`
      tip.innerHTML =
        `<div style="color:#8b949e">${localCrosshairTimeFormatter(param.time as UTCTimestamp)}</div>` +
        `<div>Cumulative P&L <b>${formatUsd(pt.cumulative_pnl)}</b></div>`
    })
    chartRef.current = chart
    seriesRef.current = series
    return () => {
      chart.remove()
      chartRef.current = null
      seriesRef.current = null
    }
  }, [])

  // Push data whenever points change.
  useEffect(() => {
    const series = seriesRef.current
    if (!series) return
    const bySecond = new Map<number, PnlCurvePoint>()
    for (const p of points) {
      bySecond.set(Math.floor(p.ts), p)
    }
    bySecondRef.current = bySecond
    const data = [...bySecond.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([time, p]) => ({ time: time as UTCTimestamp, value: p.cumulative_pnl }))
    series.setData(data)
    chartRef.current?.timeScale().fitContent()
  }, [points])

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
