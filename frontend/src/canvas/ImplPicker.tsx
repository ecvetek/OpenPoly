/**
 * Canvas variant selector — lets a node choose which of the (possibly
 * several) catalog-registered implementations for its sectionType actually
 * runs. Rendered only when more than one entry exists for that type (see
 * ConfigTab.tsx); a section with exactly one impl (the common case today)
 * never shows this control.
 *
 * Switching resets the node's config to the new impl's schema defaults —
 * ConfigTab's own `updateNodeImpl` call does that, not this component —
 * since two impls' Config shapes aren't guaranteed compatible.
 */
import type { RuntimeCatalogEntry } from '../sections/types'

const SELECT_CLASS =
  'rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 text-[11px] text-neutral-300'

export function ImplPicker({
  entries,
  selected,
  onChange,
}: {
  entries: RuntimeCatalogEntry[]
  selected: RuntimeCatalogEntry
  onChange: (entry: RuntimeCatalogEntry) => void
}) {
  return (
    <label className="flex items-center gap-2 text-[11px] text-neutral-400">
      <span>implementation</span>
      <select
        aria-label="Implementation"
        className={SELECT_CLASS}
        value={`${selected.module}::${selected.name}`}
        onChange={(e) => {
          const next = entries.find(
            (entry) => `${entry.module}::${entry.name}` === e.target.value,
          )
          if (next) onChange(next)
        }}
      >
        {entries.map((entry) => (
          <option key={`${entry.module}::${entry.name}`} value={`${entry.module}::${entry.name}`}>
            {entry.name}
          </option>
        ))}
      </select>
    </label>
  )
}
