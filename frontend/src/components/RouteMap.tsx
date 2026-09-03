import { useEffect, useMemo } from 'react'
import { MapContainer, Marker, Polyline, Popup, TileLayer, useMap } from 'react-leaflet'
import L from 'leaflet'
// Imported beside the library rather than in main.tsx, so the ~14 kB of Leaflet CSS
// is emitted with the lazily-loaded map chunk instead of the stylesheet every page
// blocks on.
import 'leaflet/dist/leaflet.css'

import { cn } from '@/lib/cn'

export interface MapMarker {
  lat: number
  lon: number
  label: string
  kind?: 'plate' | 'vehicle' | 'recording' | 'event'
  onClick?: () => void
}

interface Props {
  /** One line, or several. Several is the honest shape for a route with gaps in it. */
  route?: [number, number][] | [number, number][][]
  markers?: MapMarker[]
  start?: [number, number] | null
  end?: [number, number] | null
  tileUrl?: string
  attribution?: string
  maxZoom?: number
  className?: string
  /** Index is within its segment, so the caller can look the point back up. */
  onPointClick?: (lat: number, lon: number, index: number, segment: number) => void
}

const DEFAULT_TILES = 'https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png'
const DEFAULT_ATTRIBUTION = '&copy; OpenStreetMap contributors'

/**
 * Leaflet's default icons resolve their PNGs relative to the stylesheet, which bundlers
 * rewrite and a strict CSP blocks. Building the markers from inline SVG avoids shipping
 * image assets at all and keeps them themeable.
 */
function dot(color: string, size = 12) {
  return L.divIcon({
    className: 'dashcam-marker',
    html: `<span style="display:block;width:${size}px;height:${size}px;border-radius:50%;
           background:${color};border:2px solid rgba(255,255,255,.85);
           box-shadow:0 0 0 1px rgba(0,0,0,.3)"></span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  })
}

const ICONS = {
  start: dot('#10b981', 16),
  end: dot('#ef4444', 16),
  plate: dot('#00f0ff'),
  vehicle: dot('#a855f7'),
  recording: dot('#ff6b00'),
  event: dot('#ff3b30'),
}

function FitBounds({ points }: { points: [number, number][] }) {
  const map = useMap()
  useEffect(() => {
    if (points.length === 0) return
    if (points.length === 1) {
      map.setView(points[0]!, 16)
      return
    }
    map.fitBounds(L.latLngBounds(points), { padding: [24, 24] })
  }, [map, points])
  return null
}

/**
 * Thins a dense marker set by keeping at most one per grid cell.
 *
 * A long journey can produce thousands of plate sightings; putting each one in the DOM
 * makes panning unusable, and at any readable zoom they overlap into a single blob anyway.
 */
function decimate(markers: MapMarker[], limit = 400): MapMarker[] {
  if (markers.length <= limit) return markers
  const seen = new Set<string>()
  const kept: MapMarker[] = []
  for (const marker of markers) {
    // ~11 m cells, matching the precision the source telemetry actually carries.
    const key = `${marker.lat.toFixed(4)},${marker.lon.toFixed(4)}`
    if (seen.has(key)) continue
    seen.add(key)
    kept.push(marker)
    if (kept.length >= limit) break
  }
  return kept
}

export default function RouteMap({
  route = [],
  markers = [],
  start,
  end,
  tileUrl = DEFAULT_TILES,
  attribution = DEFAULT_ATTRIBUTION,
  maxZoom = 19,
  className,
  onPointClick,
}: Props) {
  // A route arrives either as one line or as several, and the several are the truthful
  // shape: signal drops under cover and clips can be minutes apart, so a journey is a set
  // of segments. A flat array is accepted as a single segment for callers that have one.
  const segments = useMemo<[number, number][][]>(
    () => (route.length > 0 && Array.isArray(route[0]![0]) ? route : [route]) as [number, number][][],
    [route],
  )

  // Defend against callers passing points with no fix: the camera prints zeros when it
  // has no lock, and plotting those would drag the map into the Atlantic.
  const cleanSegments = useMemo(
    () =>
      segments
        .map((segment) =>
          segment.filter(
            ([lat, lon]) =>
              Number.isFinite(lat) && Number.isFinite(lon) && (lat !== 0 || lon !== 0),
          ),
        )
        .filter((segment) => segment.length > 0),
    [segments],
  )
  const cleanRoute = useMemo(() => cleanSegments.flat(), [cleanSegments])
  const cleanMarkers = useMemo(
    () => decimate(markers.filter((m) => Number.isFinite(m.lat) && Number.isFinite(m.lon) && (m.lat !== 0 || m.lon !== 0))),
    [markers],
  )

  const allPoints = useMemo<[number, number][]>(
    () => [...cleanRoute, ...cleanMarkers.map((m) => [m.lat, m.lon] as [number, number])],
    [cleanRoute, cleanMarkers],
  )

  if (allPoints.length === 0) {
    return (
      <div className={cn('flex items-center justify-center rounded-lg border border-border bg-surface-sunken text-sm text-content-muted', className)}>
        No GPS data for this view
      </div>
    )
  }

  return (
    <MapContainer
      center={allPoints[0]}
      zoom={14}
      scrollWheelZoom
      className={cn('rounded-lg', className)}
    >
      <TileLayer url={tileUrl} attribution={attribution} maxZoom={maxZoom} />
      <FitBounds points={allPoints} />

      {cleanSegments.map((segment, segmentIndex) =>
        segment.length > 1 ? (
          <Polyline
            key={segmentIndex}
            positions={segment}
            pathOptions={{ color: '#ff6b00', weight: 4.5, opacity: 0.95 }}
            eventHandlers={
              onPointClick
                ? {
                    click: (e) => {
                      // Snap to the nearest recorded fix so the caller can map the click
                      // back to a specific moment in a recording.
                      const { lat, lng } = e.latlng
                      let best = 0
                      let bestDistance = Infinity
                      segment.forEach(([plat, plon], index) => {
                        const d = (plat - lat) ** 2 + (plon - lng) ** 2
                        if (d < bestDistance) {
                          bestDistance = d
                          best = index
                        }
                      })
                      const point = segment[best]!
                      onPointClick(point[0], point[1], best, segmentIndex)
                    },
                  }
                : undefined
            }
          />
        ) : null,
      )}

      {start && <Marker position={start} icon={ICONS.start}><Popup>Start</Popup></Marker>}
      {end && <Marker position={end} icon={ICONS.end}><Popup>Finish</Popup></Marker>}

      {cleanMarkers.map((marker, index) => (
        <Marker
          key={`${marker.lat}-${marker.lon}-${index}`}
          position={[marker.lat, marker.lon]}
          icon={ICONS[marker.kind ?? 'plate']}
          eventHandlers={marker.onClick ? { click: marker.onClick } : undefined}
        >
          <Popup>{marker.label}</Popup>
        </Marker>
      ))}
    </MapContainer>
  )
}
