/**
 * Shared rjsf plumbing for rendering a section's Pydantic-derived
 * `param_schema` as an editable form — extracted from ConfigTab.tsx so the
 * backtest page's entry/exit config pickers render identically to the
 * canvas (same `*_ref` → RefWidget / array-of-strings → TagListWidget
 * routing) without duplicating the schema-walking logic.
 */
import type { RegistryWidgetsType, UiSchema } from '@rjsf/utils'

import { RefWidget } from './RefWidget'
import { TagListWidget } from './TagListWidget'

// Any config field whose key ends in `_ref` is a secret reference — render it
// with the stored-key picker instead of a raw text input.
export const SCHEMA_FORM_WIDGETS: RegistryWidgetsType = {
  refWidget: RefWidget,
  tagListWidget: TagListWidget,
}

// A JSON Schema node, loosely typed to match `param_schema`'s own
// `Record<string, unknown>` shape (see sections/types.ts).
export type SchemaNode = Record<string, unknown>

export function resolveSchemaRef(node: SchemaNode, defs: Record<string, SchemaNode>): SchemaNode {
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
export function buildSchemaFormUiSchema(
  node: SchemaNode,
  defs: Record<string, SchemaNode>,
): UiSchema {
  const resolved = resolveSchemaRef(node, defs)
  const props = (resolved.properties ?? {}) as Record<string, SchemaNode>
  const ui: UiSchema = {}
  for (const [key, rawChild] of Object.entries(props)) {
    const child = resolveSchemaRef(rawChild, defs)
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
      const nested = buildSchemaFormUiSchema(child, defs)
      if (Object.keys(nested).length > 0) ui[key] = nested
    }
  }
  return ui
}
