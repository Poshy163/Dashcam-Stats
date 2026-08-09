import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { MapContainer, TileLayer, useMap } from 'react-leaflet'
import L from 'leaflet'
import { useEffect } from 'react'

import HeatLayer from '@/components/HeatLayer'
import RouteLines from '@/components/RouteLines'
import Spinner from '@/components/Spinner'
import { DerivedHint, EmptyState, ErrorState, PageHeader, StatTile } from '@/components/ui'
import { api } from '@/lib/api'
import { formatDuration } from '@/lib/format'

const TILES = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const ATTRIBUTION = '&copy; OpenStreetMap contributors'

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

function FitToData({ points }: { points: [number, number, number][] }) {
  const map = useMap()
  useEffect(() => {
    if (points.length === 0) return
    const bounds = L.latLngBounds(points.map(([lat, lon]) => [lat, lon] as [number, number]))
    map.fitBounds(bounds, { padding: [32, 32] })
    // Only refit when the shape of the data changes, not on every pan or filter tweak that
    // returns the same coverage -- otherwise the map yanks itself back while being read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [map, points.length])
  return null
}

export default function HeatmapPage() {
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
  const points = useMemo(() => data?.points ?? [], [data])
  const heatPoints = useMemo(
    () => points.map(([lat, lon, weight]) => ({ lat, lon, weight })),
    [points],
  )

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

      {data && points.length === 0 && !query.isLoading && (
        <EmptyState
          title="No GPS data yet"
          description={
            minSpeedKmh > 0
              ? 'No fixes above the speed filter. Try "Everywhere, including stops".'
              : 'Recordings need to finish the telemetry stage before they appear here.'
          }
        />
      )}

      {data && points.length > 0 && (
        <>
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

          <div className="overflow-hidden rounded-lg border border-border">
            <MapContainer center={[points[0]![0], points[0]![1]]} zoom={12} scrollWheelZoom className="h-[70vh] w-full">
              <TileLayer url={TILES} attribution={ATTRIBUTION} maxZoom={19} />
              <FitToData points={points} />
              {layer !== 'routes' && (
                <HeatLayer
                  points={heatPoints}
                  maxWeight={data.maxWeight}
                  precision={data.precision}
                />
              )}
              {layer !== 'heat' && routes.data && <RouteLines lines={routes.data.lines} />}
            </MapContainer>
          </div>

          <DerivedHint>
            {mode.hint} Colour is scaled logarithmically, so a road driven twice stays
            visible next to a driveway parked in for hours.
            {layer !== 'heat' && routes.data && (
              <>
                {' '}
                Routes are {routes.data.segments.toLocaleString()} traced lines across{' '}
                {routes.data.journeys} journeys, broken wherever the signal was lost rather
                than joined across the gap.
                {routes.data.truncated && ' Not all of them fit; some are not shown.'}
              </>
            )}
            {data.truncated && ' Showing the densest cells only — zoom the grid out for full coverage.'}
          </DerivedHint>
        </>
      )}
    </div>
  )
}
