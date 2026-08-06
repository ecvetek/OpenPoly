export function StatCard({
  label,
  value,
  tone,
  sub,
  pctValue,
  pctTone,
  size = 'default',
}: {
  label: string
  value: string
  tone: string
  sub?: string
  // Optional percent companion shown next to `value` (e.g. "+8.2%") — for
  // dollar-figure cards that also have a meaningful cost-basis return.
  // Separate from `sub`, which is muted/uncolored and used for plain
  // context text ("opened in range").
  pctValue?: string
  pctTone?: string
  // 'lg' bumps the value/label type up a notch for glance-from-a-distance
  // contexts (the Live TV dashboard's hero row). Defaults to the original
  // sizing so every existing call site is unaffected.
  size?: 'default' | 'lg'
}) {
  return (
    <div className={`rounded border border-neutral-800 ${size === 'lg' ? 'px-4 py-4' : 'px-3 py-2.5'}`}>
      <div
        className={`uppercase tracking-wide text-neutral-500 ${size === 'lg' ? 'text-xs' : 'text-[10px]'}`}
      >
        {label}
      </div>
      <div
        className={`mt-1 font-mono font-semibold ${tone} ${size === 'lg' ? 'text-4xl' : 'text-xl'}`}
      >
        {value}
        {pctValue !== undefined && (
          <span className={`ml-1.5 text-xs font-mono ${pctTone ?? tone}`}>
            ({pctValue})
          </span>
        )}
      </div>
      {sub !== undefined && (
        <div className="mt-0.5 text-[10px] font-mono text-neutral-500">
          {sub}
        </div>
      )}
    </div>
  )
}
