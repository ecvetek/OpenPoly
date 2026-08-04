/**
 * Demo route registry.
 *
 * The aggregate list of mock routes the demo server serves. M1 ships it empty;
 * fixture modules add their routes here:
 *   - M2 (canvas)     → ./canvas
 *   - M3 (activity)   → ./activity
 *   - Statistics page → ./statistics
 * Each module exports a `MockRoute[]` that gets spread below.
 */
import type { MockRoute } from '../mockServer'
import { canvasRoutes } from './canvas'
import { activityRoutes } from './activity'
import { statisticsRoutes } from './statistics'

export const routes: MockRoute[] = [...canvasRoutes, ...activityRoutes, ...statisticsRoutes]
