import { lazy, Suspense, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import RouteContentReady from '@/components/RouteContentReady'
import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDuration } from '@/lib/format'
import { mapBounds } from '@/lib/mapBounds'
import { useMapSettings } from '@/lib/useMapSettings'

// Start both map queries while the browser downloads Leaflet and its heat plugin.
const HeatmapCanvas = lazy(() => import('@/components/HeatmapCanvas'))

/** Grid resolutions offered, with what each is actually useful for. */
const RESOLUTIONS = [
  { value: 2, label: 'Region (~1.1 km)' },
  { value: 3, label: 'Suburb (~110 m)' },
  { value: 4, label: 'Street (~11 m)' },
]

/**
 * The overlay samples once a second whether or not the car is moving, so a parked hour is
 * 3,600 fixes in one cell. Filtering by speed is what separates "roads I drive" from
 * "places I park", and both are legitimate things to want to see.
 */
const MODES = [
  { value: 5, label: 'Routes driven', hint: 'Ignores fixes below 5 km/h, so parking does not drown out roads.' },
  { value: 0, label: 'Everywhere, including stops', hint: 'Every fix, so time spent parked shows as the hottest points.' },
]

/**
 * The heat blurs by design, so it says how often but not exactly which road. The lines are
 * the paths themselves. Shown together they answer both at once, which is why "Both" is
 * the default rather than a choice the user has to discover.
 */
const LAYERS = [
  { value: 'both', label: 'Heat + routes' },
  { value: 'heat', label: 'Heat only' },
  { value: 'routes', label: 'Routes only' },
] as const

type Layer = (typeof LAYERS)[number]['value']

export default function HeatmapPage() {
  const maps = useMapSettings()
  const [precision, setPrecision] = useState(3)
  const [minSpeedKmh, setMinSpeedKmh] = useState(5)
  const [layer, setLayer] = useState<Layer>('both')

  const query = useQuery({
    queryKey: ['heatmap', precision, minSpeedKmh],
    queryFn: () => api.map.heatmap({ precision, minSpeedKmh }),
    // The aggregate changes only when footage is processed, and it is the most expensive
    // read in the app; refetching it on every window focus is wasted work.
    staleTime: 5 * 60 * 1000,
  })

  const routes = useQuery({
    queryKey: ['map-routes'],
    queryFn: () => api.map.routes(),
    // Independent of the heat filters on purpose: the roads driven do not change when the
    // grid resolution or the speed filter does, so refetching them would be wasted work.
    staleTime: 5 * 60 * 1000,
    enabled: layer !== 'heat',
  })

  const data = query.data
  const points = data?.points
  const lines = routes.data?.lines
  const heatBounds = useMemo(() => points ? mapBounds([points]) : [], [points])
  const routeBounds = useMemo(() => lines ? mapBounds(lines) : [], [lines])
  const fitPoints = layer === 'heat' ? heatBounds : layer === 'routes' ? routeBounds : [...heatBounds, ...routeBounds]

  const mode = MODES.find((m) => m.value === minSpeedKmh) ?? MODES[0]!

  return (
    <div className="space-y-4">
      <PageHeader
        title="Heatmap"
        subtitle={data ? `${data.cells.toLocaleString()} grid cells from ${data.totalPoints.toLocaleString()} GPS fixes` : undefined}
        actions={
          <>
            <select
              className="input w-auto"
              value={layer}
              onChange={(e) => setLayer(e.target.value as Layer)}
              aria-label="Map layers"
            >
              {LAYERS.map((l) => (
                <option key={l.value} value={l.value}>
                  {l.label}
                </option>
              ))}
            </select>
            <select
              className="input w-auto"
              value={minSpeedKmh}
              onChange={(e) => setMinSpeedKmh(Number(e.target.value))}
              aria-label="What to show"
            >
              {MODES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
            <select
              className="input w-auto"
              value={precision}
              onChange={(e) => setPrecision(Number(e.target.value))}
              aria-label="Grid resolution"
            >
              {RESOLUTIONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </select>
          </>
        }
      />

      {query.isError && <ErrorState error={query.error} retry={() => query.refetch()} />}

      {query.isLoading && <Spinner label="Aggregating GPS fixes…" className="py-24" />}
      {layer !== 'heat' && routes.isLoading && <Spinner label="Loading traced routes…" className="py-8" />}
      {layer !== 'heat' && routes.isError && <ErrorState title="Could not load traced routes" error={routes.error} retry={() => routes.refetch()} />}
      {maps.query.isPending && <Spinner label="Loading map provider…" className="py-8" />}
      {maps.query.isError && <ErrorState title="Could not load map settings" error={maps.query.error} retry={() => maps.query.refetch()} />}

      {data && fitPoints.length === 0 && !query.isLoading && (layer === 'heat' || routes.isSuccess) && (
        <EmptyState
          title="No GPS data yet"
          description={
            minSpeedKmh > 0
              ? 'No fixes above the speed filter. Try "Everywhere, including stops".'
              : 'Recordings need to finish the telemetry stage before they appear here.'
          }
        />
      )}

      {data && (
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Grid cells" value={data.cells.toLocaleString()} />
          <StatTile
            label="Time represented"
            value={formatDuration(data.totalPoints)}
            hint="One GPS fix per second of footage"
          />
          <StatTile
            label="Busiest cell"
            value={formatDuration(data.maxWeight)}
            hint="Longest time spent inside a single cell"
          />
          <StatTile
            label="Average speed"
            value={data.averageSpeedKmh != null ? `${data.averageSpeedKmh} km/h` : '—'}
          />
        </div>
      )}

      <Suspense fallback={fitPoints.length > 0 && maps.query.isSuccess ? <Spinner label="Loading map…" /> : null}>
        <RouteContentReady>
          <HeatmapCanvas
            fitPoints={fitPoints}
            settingsLoaded={maps.query.isSuccess}
            tileUrl={maps.tileUrl}
            attribution={maps.attribution}
            maxZoom={maps.maxZoom}
            heat={layer !== 'routes' ? data : undefined}
            routes={layer !== 'heat' ? routes.data : undefined}
          />
        </RouteContentReady>
      </Suspense>

      <DerivedHint>
        {layer !== 'routes' && <>{mode.hint} Colour is scaled logarithmically, so a road driven twice stays
        visible next to a driveway parked in for hours. </>}
        {layer === 'routes' && 'Routes show traced roads independently of the heat grid and speed filters. '}
        {layer !== 'heat' && routes.data && (
          <>
            {' '}
            Routes are {routes.data.segments.toLocaleString()} traced lines across{' '}
            {routes.data.journeys} journeys, broken wherever the signal was lost rather
            than joined across the gap.
            {routes.data.truncated && ' Not all of them fit; some are not shown.'}
          </>
        )}
        {layer !== 'routes' && data?.truncated && ' Showing the densest cells only — zoom the grid out for full coverage.'}
      </DerivedHint>
    </div>
  )
}
