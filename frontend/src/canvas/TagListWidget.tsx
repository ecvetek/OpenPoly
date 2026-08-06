/**
 * RJSF widget for array-of-strings fields (e.g. exclude_event_tags) — a
 * tag-chip editor. Replaces rjsf's default array template, whose add/remove
 * buttons depend on Bootstrap CSS + the glyphicon webfont (neither shipped
 * here) and render as essentially invisible controls.
 */
import type { WidgetProps } from '@rjsf/utils'
import { useState } from 'react'

export function TagListWidget(props: WidgetProps) {
  const { value, onChange, disabled, readonly } = props
  const tags: string[] = Array.isArray(value) ? value : []
  const locked = Boolean(disabled || readonly)
  const [draft, setDraft] = useState('')

  const commit = () => {
    const trimmed = draft.trim()
    setDraft('')
    if (!trimmed || tags.includes(trimmed)) return
    onChange([...tags, trimmed])
  }

  const remove = (idx: number) => {
    onChange(tags.filter((_, i) => i !== idx))
  }

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1">
        {tags.length === 0 && (
          <span className="text-[11px] text-neutral-600">No tags</span>
        )}
        {tags.map((tag, idx) => (
          <span
            key={`${tag}-${idx}`}
            className="flex items-center gap-1 rounded border border-neutral-700/50 bg-neutral-800 px-1.5 py-0.5 font-mono text-[10px] text-neutral-400"
          >
            {tag}
            {!locked && (
              <button
                type="button"
                onClick={() => remove(idx)}
                aria-label={`Remove ${tag}`}
                className="text-neutral-500 hover:text-neutral-200"
              >
                ×
              </button>
            )}
          </span>
        ))}
      </div>
      {!locked && (
        <input
          type="text"
          value={draft}
          placeholder="Add a tag…"
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ',') {
              e.preventDefault()
              commit()
            }
          }}
        />
      )}
    </div>
  )
}
