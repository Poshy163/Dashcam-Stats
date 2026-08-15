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

/** Who the app thinks it is talking to. The shell renders off this and nothing else. */
export const AUTH_STATE_KEY = ['auth-state'] as const

/**
 * Forget everything fetched as somebody else, then find out who we are now.
 *
 * Used on both sides of an identity change — signing in and signing out — because both
 * need the same two things and neither can have them in the obvious order.
 *
 * `queryClient.clear()` looks like the way to do the forgetting, and it quietly prevents
 * the finding out: it removes the auth-state query along with everything else, and
 * invalidating a key that is no longer in the cache matches nothing, so nothing refetches
 * and the observer the shell is rendering from is never told anything changed. The app
 * went on displaying its last answer — the login page after a successful sign-in, the
 * whole app after a sign-out — until the tab was reloaded by hand.
 *
 * So auth-state is deliberately kept and refetched explicitly, and everything else goes.
 * Awaited, so a caller can leave its button in the "signing in" state until the shell is
 * genuinely ready to swap rather than flashing the login form one more time.
 */
export async function resetForIdentityChange(client: QueryClient): Promise<void> {
  client.removeQueries({ predicate: (query) => query.queryKey[0] !== AUTH_STATE_KEY[0] })
  await client.refetchQueries({ queryKey: AUTH_STATE_KEY })
}
