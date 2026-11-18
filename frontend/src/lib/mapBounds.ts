/** Two corners cover an entire overlay, including separate routes across signal gaps. */
export function mapBounds(lines: [number, number, ...number[]][][]): [number, number][] {
  let south = Infinity, west = Infinity, north = -Infinity, east = -Infinity
  for (const line of lines) {
    for (const [lat, lon] of line) {
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || (lat === 0 && lon === 0)) continue
      south = Math.min(south, lat)
      west = Math.min(west, lon)
      north = Math.max(north, lat)
      east = Math.max(east, lon)
    }
  }
  return south === Infinity ? [] : [[south, west], [north, east]]
}
