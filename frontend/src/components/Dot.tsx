/**
 * Small colored status dot — used across the Strategy status bar, per-section
 * status indicators, and the Health page's subsystem cards.
 */
export function Dot({ color, pulse }: { color: string; pulse?: boolean }) {
  return (
    <span
      className={`inline-block h-2 w-2 rounded-full ${pulse ? 'animate-pulse' : ''}`}
      style={{ background: color }}
    />
  )
}
