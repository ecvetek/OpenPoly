/**
 * Position price chart — a single CLOB-sourced mid-price line spanning the
 * position's whole lifetime (open through close and on to resolution),
 * always at one consistent density regardless of open/closed status — see
 * `get_position_price_history` in openpoly/api/portfolio_routes.py for why
 * that matters (splicing local order-book snapshots with a CLOB backfill
 * used to produce a visible "seam" at the close point). Entry/exit/resolved
 * markers sit on the line. No depth/spread band — dropped in favor of a
 * single clean line, matching Polymarket's own chart.
 */
import { useEffect, useMemo, useRef } from 'react'
import {
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type MouseEventParams,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { localCrosshairTimeFormatter, localTickMarkFormatter } from './chartTimeFormat'
import type { PricePoint } from './orderBookClient'

export type OrderBookChartProps = {
  priceHistory: PricePoint[]
  // Current timeframe selection — used only to detect a user-driven window
  // change and force exactly one fresh fitContent() for it; not rendered
  // (the selector buttons live in PositionDetail).
  window: string
  entry: { ts: number; price: number } | null
  exit: { ts: number; price: number } | null
  // When the market has resolved, marks that instant on the line.
  resolved?: { ts: number; winningSide: 'yes' | 'no' | null } | null
}

type MidPoint = { time: UTCTimestamp; value: number }

// Lifecycle stages the chart auto-fits to, in order. Ranked so the fit
// effect only ever moves forward (open → closed → resolved) on its own,
// never re-fitting for a stage it's already reached — a window change is a
// separate, always-fires-once trigger (see FitTracker below).
type FitStage = 'none' | 'open' | 'closed' | 'resolved'
const FIT_RANK: Record<FitStage, number> = { none: 0, open: 1, closed: 2, resolved: 3 }
type FitTracker = { rank: number; window: string }

function derive(priceHistory: PricePoint[]): { mid: MidPoint[]; bySecond: Map<number, number> } {
  // Dedupe by whole second (lightweight-charts needs strictly-increasing
  // integer time); keep the last point in each second.
  const bySecond = new Map<number, number>()
  for (const [ts, value] of priceHistory) bySecond.set(Math.floor(ts), value)
  const mid: MidPoint[] = [...bySecond.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([time, value]) => ({ time: time as UTCTimestamp, value }))
  return { mid, bySecond }
}

export function OrderBookChart({
  priceHistory,
  window,
  entry,
  exit,
  resolved = null,
}: OrderBookChartProps) {
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const midRef = useRef<ISeriesApi<'Line'> | null>(null)
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null)
  const bySecondRef = useRef<Map<number, number>>(new Map())
  // Auto-fit only at lifecycle transitions and explicit window changes — not
  // on every poll tick, which would otherwise repeatedly re-zoom as the
  // backend's cached CLOB window quietly grows in the background.
  const fittedRef = useRef<FitTracker>({ rank: 0, window: '' })

  const { mid, bySecond } = useMemo(() => derive(priceHistory), [priceHistory])

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
    const midSeries = chart.addSeries(LineSeries, {
      color: '#58a6ff',
      lineWidth: 2,
      priceFormat: { type: 'price', precision: 3, minMove: 0.001 },
    })
    const markers = createSeriesMarkers(midSeries, [])
    chart.subscribeCrosshairMove((param: MouseEventParams) => {
      const tip = tooltipRef.current
      if (!tip) return
      if (param.time === undefined || param.point === undefined) {
        tip.style.display = 'none'
        return
      }
      const price = bySecondRef.current.get(param.time as number)
      if (price === undefined) {
        tip.style.display = 'none'
        return
      }
      tip.style.display = 'block'
      tip.style.left = `${param.point.x + 12}px`
      tip.style.top = `${param.point.y + 12}px`
      tip.innerHTML =
        `<div style="color:#8b949e">${localCrosshairTimeFormatter(param.time as UTCTimestamp)}</div>` +
        `<div>${price.toFixed(3)}</div>`
    })
    chartRef.current = chart
    midRef.current = midSeries
    markersRef.current = markers
    return () => {
      chart.remove()
      chartRef.current = null
      midRef.current = null
      markersRef.current = null
    }
  }, [])

  // Push data whenever the derived series change.
  useEffect(() => {
    bySecondRef.current = bySecond
    if (!midRef.current) return
    midRef.current.setData(mid)
    const markers: SeriesMarker<Time>[] = []
    if (entry) {
      markers.push({
        time: Math.floor(entry.ts) as UTCTimestamp,
        position: 'belowBar',
        color: '#3fb950',
        shape: 'arrowUp',
        text: `entry ${entry.price.toFixed(3)}`,
      })
    }
    if (exit) {
      markers.push({
        time: Math.floor(exit.ts) as UTCTimestamp,
        position: 'aboveBar',
        color: '#f85149',
        shape: 'arrowDown',
        text: `exit ${exit.price.toFixed(3)}`,
      })
    }
    if (resolved) {
      markers.push({
        time: Math.floor(resolved.ts) as UTCTimestamp,
        position: 'aboveBar',
        color: '#8b949e',
        shape: 'circle',
        text: resolved.winningSide ? `resolved: ${resolved.winningSide}` : 'resolved',
      })
    }
    markersRef.current?.setMarkers(markers.sort((a, b) => (a.time as number) - (b.time as number)))
    const stage: FitStage = resolved ? 'resolved' : exit ? 'closed' : mid.length > 0 ? 'open' : 'none'
    const rank = FIT_RANK[stage]
    const windowChanged = fittedRef.current.window !== window
    if (rank > fittedRef.current.rank || windowChanged) {
      chartRef.current?.timeScale().fitContent()
      fittedRef.current = { rank: Math.max(rank, fittedRef.current.rank), window }
    }
  }, [mid, bySecond, entry, exit, resolved, window])

  return (
    <div className="relative h-72 w-full">
      <div ref={containerRef} className="h-full w-full" />
      <div
        ref={tooltipRef}
        className="pointer-events-none absolute z-10 hidden rounded border border-neutral-700 bg-neutral-900/95 px-2 py-1 font-mono text-[10px] text-neutral-200"
      />
    </div>
  )
}
