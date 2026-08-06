import {
  defaultConfigForType,
  MOCK_RUNTIME_CATALOG,
} from '../sections/catalog'
import type { SectionImplRef, SectionType } from '../sections/types'
import { TEMPLATE_VERSION, type Template } from './templateIO'

// Pin every seed node to MOCK_RUNTIME_CATALOG's one entry per type — the
// project's own intended baseline impl for each section, not whichever
// catalog entry happens to sort first once a second impl exists (e.g.
// ScaleOutExitV0 sorts before ThresholdExitV0 alphabetically within
// openpoly.sections.exit). Without this, findEntry()'s first-match fallback
// (for a node with no recorded impl) could silently show a DIFFERENT
// section's config form than the one the backend's own hardcoded fallback
// actually runs — a real display/behavior mismatch, not just cosmetic.
function implFor(type: SectionType): SectionImplRef | undefined {
  const entry = MOCK_RUNTIME_CATALOG.find((e) => e.type === type)
  return entry ? { module: entry.module, name: entry.name } : undefined
}

export const SEED_TEMPLATE: Template = {
  version: TEMPLATE_VERSION,
  name: 'demo baseline',
  nodes: [
    {
      id: 'news_source-seed',
      sectionType: 'news_source',
      position: { x: 0, y: -300 },
      config: defaultConfigForType('news_source', MOCK_RUNTIME_CATALOG),
      impl: implFor('news_source'),
    },
    {
      id: 'market_source-seed',
      sectionType: 'market_source',
      position: { x: 340, y: -300 },
      config: defaultConfigForType('market_source', MOCK_RUNTIME_CATALOG),
      impl: implFor('market_source'),
    },
    {
      id: 'embedding-seed',
      sectionType: 'embedding',
      position: { x: 0, y: -100 },
      config: defaultConfigForType('embedding', MOCK_RUNTIME_CATALOG),
      impl: implFor('embedding'),
    },
    {
      id: 'database-seed',
      sectionType: 'database',
      position: { x: 340, y: -100 },
      config: defaultConfigForType('database', MOCK_RUNTIME_CATALOG),
      impl: implFor('database'),
    },
    {
      id: 'analyzer-seed',
      sectionType: 'analyzer',
      position: { x: 0, y: 100 },
      config: defaultConfigForType('analyzer', MOCK_RUNTIME_CATALOG),
      impl: implFor('analyzer'),
    },
    {
      id: 'exit-seed',
      sectionType: 'exit',
      position: { x: 340, y: 100 },
      config: defaultConfigForType('exit', MOCK_RUNTIME_CATALOG),
      impl: implFor('exit'),
    },
    {
      id: 'entry-seed',
      sectionType: 'entry',
      position: { x: 0, y: 300 },
      config: defaultConfigForType('entry', MOCK_RUNTIME_CATALOG),
      impl: implFor('entry'),
    },
  ],
  edges: [
    // Pipeline flow: news → embedding → analyzer → entry.
    { source: 'news_source-seed', target: 'embedding-seed' },
    { source: 'embedding-seed', target: 'analyzer-seed' },
    { source: 'analyzer-seed', target: 'entry-seed' },
    // Write-to-DB: each persisting section feeds the database section. The
    // embedding edge mirrors EmbeddingManager persisting the market_embedding
    // vector cache through the database section's engine.
    { source: 'market_source-seed', target: 'database-seed' },
    { source: 'news_source-seed', target: 'database-seed' },
    { source: 'embedding-seed', target: 'database-seed' },
  ],
}
