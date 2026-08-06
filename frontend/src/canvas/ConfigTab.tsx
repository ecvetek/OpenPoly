/**
 * Per-section config form. Schema-driven via @rjsf/core; extracted from the
 * old single-tab SectionInspector so N6's tab framework can mount it as one
 * tab option among others (e.g. news_source also gets a Live tab).
 *
 * `*_ref` fields render through RefWidget (v9 / SK1) — a stored-key picker —
 * so raw secrets never enter the canvas node config. The old "Save secrets"
 * button (which only half-committed the generated ref) is gone.
 */
import Form from '@rjsf/core'
import type { IChangeEvent } from '@rjsf/core'
import type { RJSFSchema, UiSchema } from '@rjsf/utils'
import validator from '@rjsf/validator-ajv8'
import { useMemo } from 'react'

import { AnalyzerTestRow } from '../sections/analyzer/AnalyzerTestRow'
import { TestConnectionRow } from '../sections/news_source/TestConnectionRow'
import { entriesForType, findEntry } from '../sections/catalog'
import { useCatalogStore } from '../sections/catalogStore'
import { ImplPicker } from './ImplPicker'
import { buildSchemaFormUiSchema, SCHEMA_FORM_WIDGETS, type SchemaNode } from './schemaForm'
import { useCanvasStore, type SectionNodeType } from './store'

export function ConfigTab({ node }: { node: SectionNodeType }) {
  const updateBulk = useCanvasStore((s) => s.updateNodeConfigBulk)
  const updateImpl = useCanvasStore((s) => s.updateNodeImpl)
  const entries = useCatalogStore((s) => s.entries)
  const source = useCatalogStore((s) => s.source)
  const status = useCatalogStore((s) => s.status)
  const error = useCatalogStore((s) => s.error)

  const sectionType = node.data.sectionType
  const implOptions = useMemo(
    () => entriesForType(sectionType, entries),
    [entries, sectionType],
  )
  const entry = useMemo(
    () => findEntry(sectionType, entries, node.data.impl),
    [entries, sectionType, node.data.impl],
  )

  const obsoleteFields = useMemo(() => {
    if (!entry) return [] as string[]
    const props = (entry.param_schema.properties ?? {}) as Record<string, unknown>
    const expected = new Set(Object.keys(props))
    return Object.keys(node.data.config).filter((k) => !expected.has(k))
  }, [entry, node.data.config])

  const uiSchema = useMemo<UiSchema>(() => {
    if (!entry) return {}
    const schema = entry.param_schema as SchemaNode
    const defs = (schema.$defs ?? {}) as Record<string, SchemaNode>
    return buildSchemaFormUiSchema(schema, defs)
  }, [entry])

  return (
    <div className="flex flex-col gap-4">
      {status === 'error' && source === 'mock' && (
        <div className="rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-200 leading-snug">
          Backend offline; rendering from mock schema. Start FastAPI to use
          runtime catalog.
          <div className="mt-1 text-amber-300/70">{error}</div>
        </div>
      )}

      {obsoleteFields.length > 0 && (
        <div className="rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-[11px] text-amber-200 leading-snug">
          Template has {obsoleteFields.length} obsolete field(s) not in current
          schema:{' '}
          <code className="text-amber-300">{obsoleteFields.join(', ')}</code>.
          Edit any field to drop them.
        </div>
      )}

      {!entry ? (
        <div className="rounded border border-red-700/50 bg-red-900/20 px-3 py-2 text-sm text-red-200">
          No schema found for type &quot;{sectionType}&quot; in {source} catalog.
        </div>
      ) : (
        <>
          {implOptions.length > 1 && (
            <ImplPicker
              entries={implOptions}
              selected={entry}
              onChange={(next) =>
                updateImpl(node.id, { module: next.module, name: next.name })
              }
            />
          )}
          <div className="openpoly-rjsf">
            <Form
              schema={entry.param_schema as RJSFSchema}
              uiSchema={uiSchema}
              widgets={SCHEMA_FORM_WIDGETS}
              formData={node.data.config}
              validator={validator}
              liveValidate
              showErrorList={false}
              onChange={(e: IChangeEvent) => {
                if (!e.formData) return
                const allowed = new Set(
                  Object.keys((entry.param_schema.properties ?? {}) as object),
                )
                const filtered = Object.fromEntries(
                  Object.entries(e.formData).filter(([k]) => allowed.has(k)),
                ) as Record<string, string | number | boolean>
                updateBulk(node.id, filtered)
              }}
            >
              <span />
            </Form>
          </div>
          {sectionType === 'analyzer' && (
            <AnalyzerTestRow config={node.data.config} />
          )}
          {sectionType === 'news_source' && (
            <TestConnectionRow config={node.data.config} />
          )}
        </>
      )}
    </div>
  )
}
