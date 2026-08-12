/** Presentation helpers. Keep formatting decisions here so units read the same everywhere. */

export function formatBytes(bytes: number | null | undefined, digits = 1): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB']
  const i = Math.min(units.length - 1, Math.floor(Math.log(Math.abs(bytes)) / Math.log(1024)))
  const value = bytes / 1024 ** i
  return `${value.toFixed(i === 0 ? 0 : digits)} ${units[i]}`
}

/** Compact duration: 45s, 2m 30s, 1h 12m. Used in tables where width is tight. */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.max(0, Math.round(seconds))
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return s % 60 ? `${m}m ${s % 60}s` : `${m}m`
  const h = Math.floor(m / 60)
  return m % 60 ? `${h}h ${m % 60}m` : `${h}h`
}

/** Player-style clock for seeking within a recording: 1:23 or 1:02:03. */
export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '--:--'
  const s = Math.max(0, Math.floor(seconds))
  const hh = Math.floor(s / 3600)
  const mm = Math.floor((s % 3600) / 60)
  const ss = s % 60
  const pad = (n: number) => String(n).padStart(2, '0')
  return hh > 0 ? `${hh}:${pad(mm)}:${pad(ss)}` : `${mm}:${pad(ss)}`
}

export function formatDistance(metres: number | null | undefined): string {
  if (metres === null || metres === undefined) return '—'
  if (metres < 1000) return `${Math.round(metres)} m`
  return `${(metres / 1000).toFixed(metres < 10_000 ? 2 : 1)} km`
}

export function formatSpeed(kmh: number | null | undefined): string {
  return kmh === null || kmh === undefined ? '—' : `${Math.round(kmh)} km/h`
}

/**
 * Coordinates at the precision the overlay actually prints — four decimal places, about
 * 11 m. Showing more digits would imply accuracy the source does not have.
 */
export function formatCoords(lat: number | null, lon: number | null): string {
  if (lat === null || lon === null) return 'No GPS fix'
  return `${lat.toFixed(4)}, ${lon.toFixed(4)}`
}

export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

/** The camera's timezone, as configured in Settings. See {@link setDisplayTimeZone}. */
let displayTimeZone: string | undefined

/**
 * Show every timestamp in the camera's own timezone rather than the viewer's.
 *
 * The backend sends UTC and the browser was rendering it in whatever zone the machine
 * happens to be set to. For a dashcam library that is wrong even when it looks right: the
 * footage, the burned-in overlay and the filenames are all in the camera's local time, so
 * a clip driven at 4pm should read as 4pm from a laptop in another country — and the queue
 * should agree with the recording it names.
 *
 * Set once from `/api/status`. Until it is, formatting falls back to the browser's zone,
 * which is the old behaviour and is right often enough to be a sane default.
 */
export function setDisplayTimeZone(zone: string | undefined): void {
  displayTimeZone = zone
}

function withZone<T extends Intl.DateTimeFormatOptions>(options: T): T {
  return displayTimeZone ? { ...options, timeZone: displayTimeZone } : options
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString(
    undefined,
    withZone({
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    }),
  )
}

export function formatDate(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(
    undefined,
    withZone({ year: 'numeric', month: 'short', day: 'numeric' }),
  )
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleTimeString(undefined, withZone({ hour: '2-digit', minute: '2-digit' }))
}

export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return '—'
  const then = new Date(iso).getTime()
  if (Number.isNaN(then)) return '—'
  const diff = Date.now() - then
  const abs = Math.abs(diff)
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  const units: [Intl.RelativeTimeFormatUnit, number][] = [
    ['second', 1000],
    ['minute', 60_000],
    ['hour', 3_600_000],
    ['day', 86_400_000],
    ['month', 2_592_000_000],
    ['year', 31_536_000_000],
  ]
  let chosen: [Intl.RelativeTimeFormatUnit, number] = units[0]!
  for (const unit of units) {
    if (abs >= unit[1]) chosen = unit
  }
  return rtf.format(-Math.round(diff / chosen[1]), chosen[0])
}

/**
 * Confidence bands for plate OCR. The product rule is that uncertain reads are shown as
 * uncertain rather than silently presented as fact, so the UI colours by band everywhere
 * a plate appears.
 */
export function confidenceBand(confidence: number): 'high' | 'medium' | 'low' {
  if (confidence >= 0.85) return 'high'
  if (confidence >= 0.6) return 'medium'
  return 'low'
}

export function realtimeFactor(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—'
  return `${value.toFixed(1)}× realtime`
}
