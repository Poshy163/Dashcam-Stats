import { useEffect, useMemo } from 'react'
import { useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet.heat'

export interface HeatPoint {
  lat: number
  lon: number
  weight: number
}

interface Props {
  points: HeatPoint[]
  /** Weight of the busiest cell, from the server, used to normalise the ramp. */
  maxWeight: number
  /** Grid resolution the server aggregated at, in decimal places. */
  precision: number
  opacity?: number
}

/**
 * Intensities are compressed logarithmically, and that is not a cosmetic choice.
 *
 * The overlay samples once a second regardless of whether the car is moving, so time spent
 * stationary lands over and over in one cell: a driveway can hold thousands of fixes while
 * a road driven twice holds two. On a linear ramp the result is one incandescent dot at
 * home and an invisible road network everywhere else — technically a correct rendering of
 * the data, and useless as a picture of where someone drives.
 *
 * A log curve keeps the ordering intact while bringing three orders of magnitude into a
 * range the eye can actually read.
 */
function intensity(weight: number, maxWeight: number): number {
  if (maxWeight <= 1) return 1
  return Math.log1p(Math.max(weight, 0)) / Math.log1p(maxWeight)
}

/**
 * Blur radius in screen pixels for a given zoom and grid resolution.
 *
 * A fixed radius cannot work across the zoom range: cells are a fixed size *on the ground*,
 * so at low zoom they collapse below one pixel and the map turns into faint specks, while
 * at high zoom they spread apart into disconnected blobs with gaps between adjacent points
 * on the same road. Deriving the radius from how large a cell currently is on screen keeps
 * neighbouring cells overlapping into a continuous line at every zoom.
 */
function radiusForZoom(zoom: number, precision: number): number {
  // Metres per grid cell, and metres per pixel at this zoom near the equator. The latitude
  // term is ignored deliberately: it changes the result by a few pixels at most, and this
  // is feeding a blur radius, not a measurement.
  const cellMetres = 111_320 / 10 ** precision
  const metresPerPixel = 156_543.03392 / 2 ** zoom
  const cellPixels = cellMetres / metresPerPixel
  // Slightly larger than a cell so adjacent cells merge rather than beading, and clamped
  // so a very coarse grid at high zoom does not paint the entire viewport.
  return Math.min(40, Math.max(8, cellPixels * 1.4))
}

export default function HeatLayer({ points, maxWeight, precision, opacity = 0.85 }: Props) {
  const map = useMap()

  const data = useMemo<[number, number, number][]>(
    () =>
      points
        .filter((p) => Number.isFinite(p.lat) && Number.isFinite(p.lon) && (p.lat !== 0 || p.lon !== 0))
        .map((p) => [p.lat, p.lon, intensity(p.weight, maxWeight)]),
    [points, maxWeight],
  )

  useEffect(() => {
    if (data.length === 0) return

    const layer = L.heatLayer(data, {
      radius: radiusForZoom(map.getZoom(), precision),
      blur: 18,
      maxZoom: 17,
      // Intensities are already normalised to 0..1 above.
      max: 1,
      minOpacity: 0.25,
      gradient: {
        0.15: '#1e3a8a',
        0.35: '#0891b2',
        0.55: '#22c55e',
        0.75: '#facc15',
        1.0: '#dc2626',
      },
    })
    layer.addTo(map)

    const container = layer as unknown as { _canvas?: HTMLCanvasElement }
    if (container._canvas) container._canvas.style.opacity = String(opacity)

    const resize = () => layer.setOptions({ radius: radiusForZoom(map.getZoom(), precision) })
    map.on('zoomend', resize)

    return () => {
      map.off('zoomend', resize)
      map.removeLayer(layer)
    }
  }, [map, data, precision, opacity])

  return null
}
