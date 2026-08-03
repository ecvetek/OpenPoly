/**
 * Overview — P&L stat cards + the equity curve. Wallet cards (cash + total
 * value) read the on-chain wallet via /api/wallet/balance — ledger P&L on
 * the left, on-chain truth on the right, so a divergence between the two
 * is visible at a glance.
 */
import { useState } from 'react'
import { EquityChart } from './EquityChart'
import { fetchEquity, type EquityResponse } from './equityClient'
import { formatPnl, pnlClass } from './format'
import { usePoll } from './usePoll'
import { fetchWalletBalance, type WalletBalance } from './walletClient'

// Equity chart refresh cadence — user-selectable. `ms: null` disables
// automatic refreshing entirely (usePoll still fetches once on mount); it's
// the default since the chart's own window/refresh state is otherwise easy
// to lose track of if it keeps silently reloading underneath you.
const REFRESH_OPTIONS: ReadonlyArray<{ readonly label: string; readonly ms: number | null }> = [
  { label: 'off', ms: null },
  { label: '3s', ms: 3000 },
  { label: '5s', ms: 5000 },
  { label: '10s', ms: 10000 },
  { label: '30s', ms: 30000 },
]

// Equity chart rolling window, in hours — mirrors the backend's
// EQUITY_WINDOW_OPTIONS_HOURS safelist in openpoly/api/portfolio_routes.py.
// An unrecognized value there silently falls back to the 24-hour default,
// so keep these two lists in sync.
const WINDOW_OPTIONS: ReadonlyArray<{ readonly label: string; readonly hours: number }> = [
  { label: '1h', hours: 1 },
  { label: '6h', hours: 6 },
  { label: '12h', hours: 12 },
  { label: '1d', hours: 24 },
  { label: '7d', hours: 24 * 7 },
  { label: '30d', hours: 24 * 30 },
]

function fmtUsd(n: number | null): string {
  return n === null ? '—' : `$${n.toFixed(2)}`
}

export function OverviewTab() {
  const [refreshMs, setRefreshMs] = useState<number | null>(REFRESH_OPTIONS[0].ms)
  const [windowHours, setWindowHours] = useState<number>(WINDOW_OPTIONS[3].hours)
  // refreshKey = windowHours: changing the window forces an immediate
  // refetch even while refresh is "off" — otherwise the fetcher closure
  // would pick up the new window silently, only visible on the next
  // scheduled poll (which, with refresh off, is never).
  const { data, status, error, refetch } = usePoll<EquityResponse>(
    () => fetchEquity(windowHours),
    refreshMs,
    windowHours,
  )
  // Backend caches for 30s — no point polling faster. A failed wallet fetch
  // must not take down the P&L cards, so it gets its own poll + null guards.
  const { data: wallet } = usePoll<WalletBalance>(fetchWalletBalance, 30000)

  if (data === null) {
    return (
      <div className="grid place-items-center p-10">
        <p className="text-sm text-neutral-400">
          {status === 'error' ? `Backend unreachable: ${error}` : 'Loading…'}
        </p>
      </div>
    )
  }

  const s = data.summary
  const noWallet = wallet !== null && !wallet.configured
  return (
    <div className="px-6 pb-6 flex flex-col gap-4">
      {status === 'error' && (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-[11px] text-red-200">
          Backend unreachable; data may be stale.
        </div>
      )}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCard label="Realized P&L" value={formatPnl(s.realized)} tone={pnlClass(s.realized)} />
        <StatCard label="Unrealized P&L" value={formatPnl(s.unrealized)} tone={pnlClass(s.unrealized)} />
        <StatCard label="Total P&L" value={formatPnl(s.total)} tone={pnlClass(s.total)} />
        <StatCard label="Open positions" value={String(s.open_positions)} tone="text-neutral-100" />
        <StatCard
          label="Wallet cash"
          value={noWallet ? 'No wallet' : fmtUsd(wallet?.usdc ?? null)}
          tone={noWallet ? 'text-neutral-500' : 'text-neutral-100'}
        />
        <StatCard
          label="Wallet value"
          value={noWallet ? 'No wallet' : fmtUsd(wallet?.total ?? null)}
          tone={noWallet ? 'text-neutral-500' : 'text-neutral-100'}
          sub={
            noWallet || wallet?.positions_value == null
              ? undefined
              : `cash ${fmtUsd(wallet?.usdc ?? null)} + positions ${fmtUsd(wallet.positions_value)}`
          }
        />
      </div>
      <div className="rounded border border-neutral-800 p-3">
        <div className="flex items-center justify-between mb-2 flex-wrap gap-2">
          <div className="text-[10px] uppercase tracking-wide text-neutral-500">
            Equity curve · realized + unrealized (mark @ bid) · windowed to the
            selection below — stat cards above are all-time
          </div>
          <div className="flex items-center gap-3 flex-wrap">
            <div className="flex items-center gap-1 text-[10px] text-neutral-500">
              <span>window</span>
              {WINDOW_OPTIONS.map((opt) => (
                <button
                  key={opt.label}
                  type="button"
                  onClick={() => setWindowHours(opt.hours)}
                  className={`rounded px-1.5 py-0.5 ${
                    windowHours === opt.hours
                      ? 'bg-blue-600 text-white'
                      : 'border border-neutral-700 text-neutral-400'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <div className="flex items-center gap-1 text-[10px] text-neutral-500">
              <span>refresh</span>
              {REFRESH_OPTIONS.map((opt) => (
                <button
                  key={opt.label}
                  type="button"
                  onClick={() => setRefreshMs(opt.ms)}
                  className={`rounded px-1.5 py-0.5 ${
                    refreshMs === opt.ms
                      ? 'bg-blue-600 text-white'
                      : 'border border-neutral-700 text-neutral-400'
                  }`}
                >
                  {opt.label}
                </button>
              ))}
            </div>
            <ManualRefreshButton onClick={refetch} />
          </div>
        </div>
        {data.points.length === 0 ? (
          <div className="h-64 grid place-items-center text-[11px] text-neutral-500">
            No trades yet.
          </div>
        ) : (
          <EquityChart points={data.points} />
        )}
      </div>
    </div>
  )
}

// Lives in the window/refresh controls row (above the chart) rather than
// floating over the chart itself — an overlaid button there covered the
// top-right price-axis labels.
function ManualRefreshButton({ onClick }: { onClick: () => void }) {
  const [spinning, setSpinning] = useState(false)

  function handleClick() {
    onClick()
    setSpinning(true)
    setTimeout(() => setSpinning(false), 500)
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      title="Refresh chart"
      aria-label="Refresh chart"
      className="rounded border border-neutral-700 p-1 text-neutral-500 hover:text-neutral-200"
    >
      <svg
        viewBox="0 0 24 24"
        width="12"
        height="12"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
        className={spinning ? 'animate-spin' : ''}
      >
        <path d="M21 12a9 9 0 1 1-2.64-6.36" />
        <path d="M21 3v6h-6" />
      </svg>
    </button>
  )
}

function StatCard({
  label,
  value,
  tone,
  sub,
}: {
  label: string
  value: string
  tone: string
  sub?: string
}) {
  return (
    <div className="rounded border border-neutral-800 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className={`mt-1 text-xl font-mono font-semibold ${tone}`}>
        {value}
      </div>
      {sub !== undefined && (
        <div className="mt-0.5 text-[10px] font-mono text-neutral-500">
          {sub}
        </div>
      )}
    </div>
  )
}
