/**
 * SectionConfigPicker — impl dropdown + rjsf config form for ONE section
 * type (entry or exit), used twice by BacktestForm. Standalone from the
 * canvas store (a backtest config is transient form state, never a
 * persisted canvas node) but reuses the exact same catalog lookup + schema
 * -form plumbing (ImplPicker, buildSchemaFormUiSchema, SCHEMA_FORM_WIDGETS)
 * ConfigTab.tsx uses, so a section's config form renders identically here
 * and on the canvas.
 */
import Form from '@rjsf/core'
import type { IChangeEvent } from '@rjsf/core'
import type { RJSFSchema, UiSchema } from '@rjsf/utils'
import validator from '@rjsf/validator-ajv8'
import { useEffect, useMemo } from 'react'

import { ImplPicker } from '../../canvas/ImplPicker'
import { buildSchemaFormUiSchema, SCHEMA_FORM_WIDGETS, type SchemaNode } from '../../canvas/schemaForm'
import { defaultConfigForType, entriesForType } from '../../sections/catalog'
import { useCatalogStore } from '../../sections/catalogStore'
import type { ConfigValues, SectionType } from '../../sections/types'
import type { SectionSelection } from './backtestClient'

export function SectionConfigPicker({
  sectionType,
  value,
  onChange,
}: {
  sectionType: SectionType
  value: SectionSelection | null
  onChange: (next: SectionSelection) => void
}) {
  const entries = useCatalogStore((s) => s.entries)
  const implOptions = useMemo(() => entriesForType(sectionType, entries), [entries, sectionType])

  // Default to the first catalog entry as soon as one is available and no
  // selection exists yet — mirrors the canvas's addSectionNode, where a
  // freshly-added node always carries an explicit impl rather than staying
  // unset.
  useEffect(() => {
    if (value !== null || implOptions.length === 0) return
    const first = implOptions[0]
    onChange({
      module: first.module,
      name: first.name,
      config: defaultConfigForType(sectionType, entries, {
        module: first.module,
        name: first.name,
      }),
    })
    // Only re-run when the catalog / available options actually change —
    // `value`/`onChange` intentionally excluded so this doesn't loop.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [implOptions])

  const selectedEntry = useMemo(
    () =>
      value
        ? implOptions.find((e) => e.module === value.module && e.name === value.name)
        : undefined,
    [implOptions, value],
  )

  const uiSchema = useMemo<UiSchema>(() => {
    if (!selectedEntry) return {}
    const schema = selectedEntry.param_schema as SchemaNode
    const defs = (schema.$defs ?? {}) as Record<string, SchemaNode>
    return buildSchemaFormUiSchema(schema, defs)
  }, [selectedEntry])

  if (implOptions.length === 0) {
    return (
      <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-xs text-red-200">
        No {sectionType} implementations found in the catalog.
      </div>
    )
  }

  if (!selectedEntry || !value) return null // waiting on the default-select effect above

  return (
    <div className="flex flex-col gap-2">
      {implOptions.length > 1 && (
        <ImplPicker
          entries={implOptions}
          selected={selectedEntry}
          onChange={(next) =>
            onChange({
              module: next.module,
              name: next.name,
              config: defaultConfigForType(sectionType, entries, {
                module: next.module,
                name: next.name,
              }),
            })
          }
        />
      )}
      <div className="openpoly-rjsf">
        <Form
          schema={selectedEntry.param_schema as RJSFSchema}
          uiSchema={uiSchema}
          widgets={SCHEMA_FORM_WIDGETS}
          formData={value.config}
          validator={validator}
          liveValidate
          showErrorList={false}
          onChange={(e: IChangeEvent) => {
            if (!e.formData) return
            onChange({ module: value.module, name: value.name, config: e.formData as ConfigValues })
          }}
        >
          <span />
        </Form>
      </div>
    </div>
  )
}
