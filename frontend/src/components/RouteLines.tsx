import { useEffect, useMemo } from 'react'
import { useMap } from 'react-leaflet'
import L from 'leaflet'

interface Props {
  /** Polylines as [lat, lon] pairs. One drive is usually several, split at signal gaps. */
  lines: number[][][]
  color?: string
  opacity?: number
  weight?: number
}

/**
 * The roads themselves, under the heat.
 *
 * The heat layer answers "how much time here" and deliberately blurs, so at any readable
 * zoom a road and the car park beside it are one warm smudge. These lines are the paths
 * actually driven, and the two complement rather than replace each other: the lines say
 * which roads, the heat says how often.
 *
 * Drawn thin and semi-transparent on purpose. Overlapping drives then accumulate — a
 * commute taken two hundred times reads brighter than a road taken once — so the overlay
 * carries frequency information of its own without needing a second colour scale.
 */
export default function RouteLines({
  lines,
  color = '#f8fafc',
  opacity = 0.42,
  weight = 1.6,
}: Props) {
  const map = useMap()

  const paths = useMemo(
    () =>
      lines
        .map((line) =>
          line
            .filter(
              ([lat, lon]) =>
                Number.isFinite(lat) && Number.isFinite(lon) && (lat !== 0 || lon !== 0),
            )
            .map(([lat, lon]) => [lat, lon] as [number, number]),
        )
        .filter((line) => line.length > 1),
    [lines],
  )

  useEffect(() => {
    if (paths.length === 0) return

    // A canvas renderer rather than Leaflet's default SVG. A library of drives is hundreds
    // of polylines and tens of thousands of vertices; as SVG that is one DOM node per line
    // and panning becomes visibly jerky, while canvas stays smooth because the browser is
    // drawing pixels rather than maintaining a document.
    const renderer = L.canvas({ padding: 0.3 })
    const group = L.layerGroup(
      paths.map((path) =>
        L.polyline(path, {
          renderer,
          color,
          weight,
          opacity,
          // Rounded joins stop the corners of a turn showing as notches at low weight.
          lineJoin: 'round',
          lineCap: 'round',
          interactive: false,
        }),
      ),
    )
    group.addTo(map)
    return () => {
      map.removeLayer(group)
    }
  }, [map, paths, color, opacity, weight])

  return null
}
