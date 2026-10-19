import { useEffect } from 'react'
import { useMap } from 'react-leaflet'

/** Follow changed geographic coverage while preserving the view across identical data. */
export default function FitMapToData({ points }: { points: [number, number][] }) {
  const map = useMap()
  let south = Infinity, west = Infinity, north = -Infinity, east = -Infinity
  for (const [lat, lon] of points) {
    if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue
    south = Math.min(south, lat)
    west = Math.min(west, lon)
    north = Math.max(north, lat)
    east = Math.max(east, lon)
  }
  useEffect(() => {
    if (!Number.isFinite(south)) return
    map.fitBounds([[south, west], [north, east]], { padding: [32, 32] })
  }, [map, south, west, north, east])
  return null
}
