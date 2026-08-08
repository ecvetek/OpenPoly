/**
 * Shared chart chrome for the two baseline-series charts in the app
 * (EquityChart, PnlCurveChart) — construction options, crosshair tooltip
 * wiring, and the de-dup-by-second requirement lightweight-charts imposes
 * (it needs strictly-increasing whole-second timestamps; points sharing a
 * second are collapsed, keeping the last). Previously ~90 of ~125 lines
 * duplicated verbatim between the two components; only the value extracted
 * per point and the tooltip content actually differ, so those are the two
 * things callers supply.
 *
 * Time-axis/crosshair labels are formatted in the browser's local timezone —
 * see chartTimeFormat.ts for why that override is necessary.
 *
 * v5 API: chart.addSeries(BaselineSeries, …). If a v4 build is installed,
 * swap to chart.addBaselineSeries(…) and remove the BaselineSeries import.
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
import { localCrosshairTimeFormatter, localTickMarkFormatter } from './chartTimeFormat'

export type TimeSeriesPoint = { ts: number }

/**
 * Renders a baseline series for `points` into the returned `containerRef`,
 * with a tooltip (rendered into `tooltipRef`) following the crosshair.
 *
 * `getValue`/`renderTooltip` are read via a ref on every crosshair move /
 * data push rather than being setup-effect dependencies, so passing a fresh
 * inline closure each render (the common case) doesn't tear down and
 * recreate the chart — only a change to `points` itself does that.
 */
export function useLightweightChart<T extends TimeSeriesPoint>(
  points: T[],
  getValue: (point: T) => number,
  renderTooltip: (point: T, time: UTCTimestamp) => string,
) {
  const containerRef = useRef<HTMLDivElement>(null)
  const tooltipRef = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const seriesRef = useRef<ISeriesApi<'Baseline'> | null>(null)
  const bySecondRef = useRef<Map<number, T>>(new Map())

  const getValueRef = useRef(getValue)
  const renderTooltipRef = useRef(renderTooltip)
  // Runs after every render (no dependency array) — cheaper than it looks
  // (two assignments) and keeps the refs current without making them
  // render-time writes, which React's rules disallow.
  useEffect(() => {
    getValueRef.current = getValue
    renderTooltipRef.current = renderTooltip
  })

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
      tip.innerHTML = renderTooltipRef.current(pt, param.time as UTCTimestamp)
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
    const bySecond = new Map<number, T>()
    for (const p of points) {
      bySecond.set(Math.floor(p.ts), p)
    }
    bySecondRef.current = bySecond
    const data = [...bySecond.entries()]
      .sort((a, b) => a[0] - b[0])
      .map(([time, p]) => ({ time: time as UTCTimestamp, value: getValueRef.current(p) }))
    series.setData(data)
    chartRef.current?.timeScale().fitContent()
  }, [points])

  return { containerRef, tooltipRef }
}
