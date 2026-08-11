import type { QueryClient } from '@tanstack/react-query'

// Views backed by pipeline output. Reanalysis retains old database rows until their
// replacements succeed, so each view must refetch after invalidation and while the queue
// repopulates. Keeping the roots here prevents one mutation remembering Vehicles while
// another remembers the map but forgets Recordings and its newly-created thumbnails.
const ANALYSIS_QUERY_ROOTS = new Set([
  'status',
  'recordings',
  'recording',
  'recording-telemetry',
  'recording-detections',
  'recording-plates',
  'journeys',
  'journey',
  'heatmap',
  'map-routes',
  'telemetry-quality',
  'plates',
  'plate',
  'plate-observations',
  'vehicles',
  'search',
])
const MAP_QUERY_ROOTS = new Set(['heatmap', 'map-routes'])

export function invalidateAnalysisQueries(
  client: QueryClient,
  options: { includeMaps?: boolean } = {},
): Promise<void> {
  const includeMaps = options.includeMaps ?? true
  return client.invalidateQueries({
    predicate: (query) => {
      const root = String(query.queryKey[0] ?? '')
      return ANALYSIS_QUERY_ROOTS.has(root) && (includeMaps || !MAP_QUERY_ROOTS.has(root))
    },
  })
}
