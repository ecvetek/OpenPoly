export type SectionType =
  | 'news_source'
  | 'market_source'
  | 'embedding'
  | 'analyzer'
  | 'entry'
  | 'exit'
  | 'database'

export type Capability =
  | 'news'
  | 'llm'
  | 'market_data'
  | 'order_book'
  | 'news_history'
  | 'portfolio'

export type ConfigValues = Record<string, string | number | boolean>

export type RuntimeCatalogEntry = {
  type: SectionType
  name: string
  version: string
  module: string
  requires: Capability[]
  param_schema: Record<string, unknown>
  source: 'builtin' | 'user'
}

// Identifies one catalog entry's concrete impl class — the (module, name)
// pair a canvas node records to say "run THIS implementation" once more
// than one exists for its sectionType (the variant selector).
export type SectionImplRef = { module: string; name: string }
