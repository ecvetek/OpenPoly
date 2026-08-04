/**
 * Shared lightweight-charts time formatters for the Activity tabs' charts
 * (EquityChart, OrderBookChart).
 *
 * IMPORTANT: lightweight-charts formats numeric (UTCTimestamp) axis/crosshair
 * labels in UTC by default — it does NOT convert to the browser's local
 * timezone on its own, despite the type name. Without these overrides, every
 * label is silently offset from the viewer's wall clock by their UTC offset
 * (e.g. a UTC+2 browser sees "19:25" labeled on a point that was actually
 * recorded at local 21:25). Both formatters below explicitly convert via
 * `new Date(ts * 1000)` + the browser's local Intl formatting, so labels
 * always match wall-clock time.
 */
import { TickMarkType, type UTCTimestamp } from 'lightweight-charts'

// Local-timezone tick label, granularity-aware (mirrors what the library's
// own default UTC formatter would show, just using local Date methods).
export function localTickMarkFormatter(time: UTCTimestamp, tickMarkType: TickMarkType): string {
  const d = new Date(time * 1000)
  switch (tickMarkType) {
    case TickMarkType.Year:
      return String(d.getFullYear())
    case TickMarkType.Month:
      return d.toLocaleDateString([], { month: 'short', year: 'numeric' })
    case TickMarkType.DayOfMonth:
      return d.toLocaleDateString([], { month: 'short', day: 'numeric' })
    case TickMarkType.TimeWithSeconds:
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
    default:
      return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })
  }
}

// Local-timezone label for the crosshair's floating time-axis readout (the
// small tag at the bottom edge under the crosshair).
export function localCrosshairTimeFormatter(time: UTCTimestamp): string {
  return new Date(time * 1000).toLocaleString([], {
    day: '2-digit',
    month: 'short',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
