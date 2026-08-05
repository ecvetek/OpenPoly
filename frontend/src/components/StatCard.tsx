export function StatCard({
  label,
  value,
  tone,
  sub,
  pctValue,
  pctTone,
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
}) {
  return (
    <div className="rounded border border-neutral-800 px-3 py-2.5">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">
        {label}
      </div>
      <div className={`mt-1 text-xl font-mono font-semibold ${tone}`}>
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
