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
import type { RegistryWidgetsType, RJSFSchema, UiSchema } from '@rjsf/utils'
import validator from '@rjsf/validator-ajv8'
import { useMemo } from 'react'

import { AnalyzerTestRow } from '../sections/analyzer/AnalyzerTestRow'
import { TestConnectionRow } from '../sections/news_source/TestConnectionRow'
import { entriesForType, findEntry } from '../sections/catalog'
import { useCatalogStore } from '../sections/catalogStore'
import { ImplPicker } from './ImplPicker'
import { RefWidget } from './RefWidget'
import { TagListWidget } from './TagListWidget'
import { useCanvasStore, type SectionNodeType } from './store'

// Any config field whose key ends in `_ref` is a secret reference — render it
// with the stored-key picker instead of a raw text input.
const WIDGETS: RegistryWidgetsType = {
  refWidget: RefWidget,
  tagListWidget: TagListWidget,
}

// A JSON Schema node, loosely typed to match `param_schema`'s own
// `Record<string, unknown>` shape (see sections/types.ts).
type SchemaNode = Record<string, unknown>

function resolveRef(node: SchemaNode, defs: Record<string, SchemaNode>): SchemaNode {
  const ref = node.$ref
  if (typeof ref === 'string') {
    const name = ref.replace('#/$defs/', '')
    return defs[name] ?? node
  }
  return node
}

// Recursively route `*_ref` fields through RefWidget and array-of-strings
// fields through TagListWidget, at any nesting depth (rjsf's default array
// UI depends on Bootstrap CSS/glyphicon fonts we don't ship — see
// TagListWidget.tsx). Resolves `$ref`s (pydantic nests sub-model schemas
// under `$defs`, e.g. market_source's `filter: MarketFilterConfig`).
function buildUiSchema(node: SchemaNode, defs: Record<string, SchemaNode>): UiSchema {
  const resolved = resolveRef(node, defs)
  const props = (resolved.properties ?? {}) as Record<string, SchemaNode>
  const ui: UiSchema = {}
  for (const [key, rawChild] of Object.entries(props)) {
    const child = resolveRef(rawChild, defs)
    if (key.endsWith('_ref')) {
      ui[key] = { 'ui:widget': 'refWidget' }
      continue
    }
    const items = child.items as SchemaNode | undefined
    if (child.type === 'array' && items?.type === 'string' && items.enum === undefined) {
      ui[key] = { 'ui:widget': 'tagListWidget' }
      continue
    }
    if (child.type === 'object' && child.properties) {
      const nested = buildUiSchema(child, defs)
      if (Object.keys(nested).length > 0) ui[key] = nested
    }
  }
  return ui
}

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
    return buildUiSchema(schema, defs)
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
              widgets={WIDGETS}
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
