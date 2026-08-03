import { Dot } from '../../components/Dot'
import { ago } from '../../lib/time'
import type { SubsystemCheck, SubsystemStatus } from './healthClient'

const STATUS_COLOR: Record<SubsystemStatus, string> = {
  ok: '#34d399',
  degraded: '#f59e0b',
  down: '#ef4444',
  stopped: '#6b7280',
  disabled: '#6b7280',
}

// Pulse draws the eye to states worth investigating; steady dot for healthy
// or intentionally-idle states.
const STATUS_PULSE: Record<SubsystemStatus, boolean> = {
  ok: false,
  degraded: true,
  down: true,
  stopped: false,
  disabled: false,
}

const STATUS_TEXT_CLASS: Record<SubsystemStatus, string> = {
  ok: 'text-emerald-300',
  degraded: 'text-amber-300',
  down: 'text-red-300',
  stopped: 'text-neutral-400',
  disabled: 'text-neutral-500',
}

function formatKey(key: string): string {
  return key.replace(/_/g, ' ')
}

// Generic — the backend's per-subsystem detail shapes are heterogeneous by
// design (and two of them are intentionally minimal today), so the card
// renders whatever it's given rather than special-casing per subsystem.
function formatDetailValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (key.endsWith('_at') && typeof value === 'number') return `${ago(value)} ago`
  if (typeof value === 'boolean') return value ? 'yes' : 'no'
  if (typeof value === 'number') return Number.isInteger(value) ? String(value) : value.toFixed(2)
  if (typeof value === 'string') return value || '—'
  const s = JSON.stringify(value)
  return s.length > 60 ? `${s.slice(0, 57)}…` : s
}

export function HealthCard({
  label,
  check,
}: {
  label: string
  check: SubsystemCheck | undefined
}) {
  if (check === undefined) {
    return (
      <div className="rounded border border-neutral-800 px-3 py-2.5">
        <div className="flex items-center gap-2">
          <Dot color={STATUS_COLOR.stopped} />
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
        </div>
        <div className="mt-1 text-[11px] text-neutral-600">no data</div>
      </div>
    )
  }

  const entries = Object.entries(check.detail)

  return (
    <div className="rounded border border-neutral-800 px-3 py-2.5">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Dot color={STATUS_COLOR[check.status]} pulse={STATUS_PULSE[check.status]} />
          <span className="text-[10px] uppercase tracking-wide text-neutral-500">{label}</span>
        </div>
        <span
          className={`text-[10px] uppercase tracking-wide font-medium ${STATUS_TEXT_CLASS[check.status]}`}
        >
          {check.status}
        </span>
      </div>
      {entries.length > 0 && (
        <div className="mt-2 flex flex-col gap-0.5">
          {entries.map(([key, value]) => (
            <div key={key} className="flex items-baseline justify-between gap-2 text-[11px] font-mono">
              <span className="text-neutral-500 truncate">{formatKey(key)}</span>
              <span className="text-neutral-200 text-right truncate">{formatDetailValue(key, value)}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
