import { useEffect, useMemo } from 'react'
import { useMap } from 'react-leaflet'
import L from 'leaflet'
import 'leaflet.heat'

interface Props {
  points: [number, number, number][]
  /** Weight of the busiest cell, from the server, used to normalise the ramp. */
  maxWeight: number
  /** Grid resolution the server aggregated at, in decimal places. */
  precision: number
  opacity?: number
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

  const data = useMemo(() => {
    const normalized: [number, number, number][] = []
    // Logarithmic intensities keep an occasional drive visible beside hours of parked fixes.
    const scale = Math.log1p(maxWeight)
    for (const [lat, lon, weight] of points) {
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || (lat === 0 && lon === 0)) continue
      normalized.push([lat, lon, maxWeight <= 1 ? 1 : Math.log1p(Math.max(weight, 0)) / scale])
    }
    return normalized
  }, [points, maxWeight])

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
